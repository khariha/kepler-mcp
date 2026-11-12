"""tess-hunt MCP server.

    tesshunt-mcp                 # streamable HTTP on 0.0.0.0:8000/mcp (hosted)
    tesshunt-mcp --stdio         # local, for Claude Code / Claude Desktop

Env: TESSHUNT_MCP_DATA (cache + traces dir), TESSHUNT_MCP_HOSTS (comma-separated allowed Host headers, or *),
     TESSHUNT_MCP_LIGHT_PER_HOUR, TESSHUNT_MCP_HEAVY_PER_HOUR, TESSHUNT_MCP_TIMEOUT_S, TESSHUNT_MCP_CONCURRENCY,
     TESSHUNT_MCP_TRACES=0 to turn the trace log off, TESSHUNT_MCP_SALT.
"""
import argparse, os, re, time, warnings
import anyio
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from tesshunt_mcp import core, store

warnings.filterwarnings("ignore")
TIMEOUT = float(os.environ.get("TESSHUNT_MCP_TIMEOUT_S", 120))
_slots = None   # created inside the event loop

INSTRUCTIONS = """tess-hunt: look for transiting planet CANDIDATES around any star TESS observed, one star at a time, from the mission's public full-frame images, and try to break every signal before believing it.

Procedure (the `hunt` prompt spells it out): resolve_star -> catalog_check -> light_curve / search_transits on one sector -> vet_signal -> neighbors -> followup_start + job_status across all sectors -> state a verdict.

Rules for whoever reads the results:
- Say "planet candidate", never "planet" or "discovered". Nothing here confirms a planet; only follow-up observations can.
- A single sector is never enough. A signal must repeat (followup_start) and survive the all-phase secondary test before it is called a candidate.
- Every number you report about a star must come from a tool run on that star in this conversation. The vetting guide (read_guide) is background, not evidence.
- A network or archive failure is an error to retry, not a verdict about the star.

Times are BTJD (BJD - 2457000). Depths are ppm. Calls are rate limited per user; cached answers are free.
Privacy: tool names, arguments and numeric results are logged with an anonymous daily-rotating id to improve the tools; the log may be used to train open models. No IP address, account, prompt or model reasoning is stored."""

mcp = MCPServer("tess-hunt", title="tess-hunt: TESS planet-candidate vetting", instructions=INSTRUCTIONS,
                website_url="https://github.com/khariha/tess-hunt-mcp", version="0.1.0")


async def _run(tool, cls, ctx, args, fn, cacheable=True):
    """Rate limit, cache, run in a worker thread with a timeout, trace. Returns MCP content."""
    global _slots
    if _slots is None:
        _slots = anyio.Semaphore(int(os.environ.get("TESSHUNT_MCP_CONCURRENCY", 4)))
    headers = dict(ctx.headers) if (ctx is not None and ctx.headers) else {}
    sid = store.session_id(headers)
    t0 = time.time()
    k = store.key(tool, args)
    hit = store.cache_get(tool, k) if cacheable else None
    if hit is not None:
        result, png = hit
        store.trace(sid, tool, args, result, time.time() - t0, True)
        return _content(result, png)
    ok, wait = store.allow(sid, cls)
    if not ok:
        msg = f"rate limit reached for {cls} calls; try again in about {max(1, wait // 60)} min"
        store.trace(sid, tool, args, None, 0, False, error=msg)
        raise ToolError(msg)
    try:
        async with _slots:
            with anyio.fail_after(TIMEOUT):
                result, png = await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
    except TimeoutError:
        msg = f"{tool} exceeded {TIMEOUT:.0f} s (archive slow or star in many sectors); retry, the pixels are cached as they arrive"
        store.trace(sid, tool, args, None, time.time() - t0, False, error=msg)
        raise ToolError(msg)
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        store.trace(sid, tool, args, None, time.time() - t0, False, error=msg)
        raise ToolError(msg) from None
    result = store.clean(result)
    if cacheable:
        store.cache_put(tool, k, result, png)
    store.trace(sid, tool, args, result, time.time() - t0, False)
    return _content(result, png)


def _content(result, png):
    return [result, Image(data=png, format="png")] if png else [result]


@mcp.tool(title="Resolve a star")
async def resolve_star(query: str, ctx: Context) -> list:
    """Find a star in the TESS Input Catalog. query: 'TIC 261136679', a bare TIC number, a name ('Pi Mensae', 'TOI-700', 'LHS 3844'), or 'ra dec' in degrees.
    Returns TIC id, coordinates, TESS magnitude, temperature, radius, mass, distance, contamination ratio, and the sectors that observed it."""
    q = query.strip()[:80]
    return await _run("resolve_star_tool", "light", ctx, {"q": q.lower()}, lambda: core.resolve_star(q))


@mcp.tool(title="Check catalogs")
async def catalog_check(tic: int, ctx: Context) -> list:
    """Is this TIC already a confirmed planet host, a TESS Object of Interest (TOI), or a Community TOI? Do this before any search:
    a signal on a known planet host is a recovery, not a new candidate."""
    return await _run("catalog_check", "light", ctx, {"tic": tic}, lambda: core.catalog_check(tic))


@mcp.tool(title="Light curve from full-frame images")
async def light_curve(tic: int, sector: int, ctx: Context) -> list:
    """Build the star's brightness-vs-time curve for one sector from raw TESS full-frame-image pixels (first call per star and sector takes 5-50 s).
    Returns cadence, noise, a binned series and a plot. Use a sector from resolve_star."""
    return await _run("light_curve", "light", ctx, {"tic": tic, "sector": sector}, lambda: core.light_curve(tic, sector))


@mcp.tool(title="Search for transits")
async def search_transits(tic: int, sector: int, ctx: Context, min_period_d: float = 0.3, max_period_d: float = 13.0,
                          min_snr: float = 9.0, min_depth_ppm: float = 0.0) -> list:
    """Box-least-squares search of one sector for a periodic dip. Returns the strongest signal (period, epoch t0, duration, depth, SNR),
    quick shape tests, whether it passes the candidate bar and why not, plus a periodogram and fold plot. Every star returns a 'best' peak;
    most are noise, so read SNR, depth_sigma and failed_checks. Narrow the period range to chase a specific signal; raise min_depth_ppm
    (for example 3000) to mimic a survey that only wants deep dips."""
    a = {"tic": tic, "sector": sector, "min_period_d": min_period_d, "max_period_d": max_period_d, "min_snr": min_snr, "min_depth_ppm": min_depth_ppm}
    return await _run("search_transits", "light", ctx, a, lambda: core.search_transits(tic, sector, min_period_d, max_period_d, min_snr, min_depth_ppm))


@mcp.tool(title="Vet a signal")
async def vet_signal(tic: int, sector: int, period_d: float, t0: float, duration_h: float, ctx: Context) -> list:
    """Try to break a signal in one sector: odd vs even depths, a secondary eclipse at ANY orbital phase (catches eccentric eclipsing binaries),
    duration against the physical maximum, centroid shift, difference image, and Gaia neighbours. Returns a verdict with reasons and a three-panel plot.
    t0 is a mid-transit time in BTJD, as returned by search_transits."""
    a = {"tic": tic, "sector": sector, "period_d": period_d, "t0": t0, "duration_h": duration_h}
    return await _run("vet_signal", "light", ctx, a, lambda: core.vet_signal(tic, sector, period_d, t0, duration_h))


@mcp.tool(title="Gaia neighbours")
async def neighbors(tic: int, sector: int, ctx: Context, depth_ppm: float | None = None) -> list:
    """Gaia DR3 stars within 3 TESS pixels (63 arcsec) of the target, with separation and brightness relative to the target.
    Give depth_ppm to see how deep an eclipse on each neighbour would have to be to fake the dip."""
    a = {"tic": tic, "sector": sector, "depth_ppm": depth_ppm}
    return await _run("neighbors", "light", ctx, a, lambda: core.neighbors(tic, sector, depth_ppm))


@mcp.tool(title="Phase-folded plot")
async def fold_plot(tic: int, sectors: list[int], period_d: float, t0: float, ctx: Context) -> list:
    """Fold up to 4 sectors at a period and epoch and overlay them. Useful to see whether the dip lines up across sectors."""
    a = {"tic": tic, "sectors": sorted(set(sectors))[:4], "period_d": period_d, "t0": t0}
    return await _run("fold_plot", "light", ctx, a, lambda: core.fold_plot(tic, a["sectors"], period_d, t0))


def _followup_args(tic, period_d, t0, duration_h, depth_ppm):
    return {"tic": int(tic), "period_d": period_d, "t0": t0, "duration_h": duration_h, "depth_ppm": depth_ppm}


@mcp.tool(title="Start multi-sector follow-up")
async def followup_start(tic: int, period_d: float, t0: float, duration_h: float, depth_ppm: float, ctx: Context) -> dict:
    """Start the decisive test: search every sector that observed the star, independently, for the same signal, then refine the period on the
    stitched baseline. Takes 1-5 minutes; returns a job_id at once. Poll job_status(job_id) about every 20 s."""
    a = _followup_args(tic, period_d, t0, duration_h, depth_ppm)
    k = store.key("followup", a)
    sid = store.session_id(dict(ctx.headers) if ctx.headers else {})
    if store.cache_get("followup", k) is None and store.job_get("followup", k)[0]["state"] not in ("queued", "running"):
        ok, wait = store.allow(sid, "heavy")
        if not ok:
            raise ToolError(f"follow-up limit reached; try again in about {max(1, wait // 60)} min")
    state = store.job_start("followup", k, lambda progress: core.followup(progress=progress, **a))
    out = {"job_id": k, "state": state, "poll": "job_status(job_id)"}
    store.trace(sid, "followup_start", a, out, 0, state == "done")
    return out


@mcp.tool(title="Follow-up job status")
async def job_status(job_id: str, ctx: Context) -> list:
    """State of a follow-up job: queued, running (with progress), failed, or done with the full result and stitched fold plot."""
    if not re.fullmatch(r"[0-9a-f]{20}", job_id):
        raise ToolError("job_id is the 20-character id returned by followup_start")
    state, hit = store.job_get("followup", job_id)
    sid = store.session_id(dict(ctx.headers) if ctx.headers else {})
    if hit is None:
        return [state]
    result, png = hit
    store.trace(sid, "job_status", {"job_id": job_id}, result, 0, True)
    return _content({"state": "done", **result}, png)


VERDICTS = ("candidate", "eclipsing_binary", "blend_or_off_target", "systematics_or_noise", "known_object", "undecided")
DECIDING = ("centroid", "secondary", "odd_even", "duration", "recovery", "catalog", "dilution", "snr", "other")


@mcp.tool(title="Record a verdict")
async def report_verdict(tic: int, verdict: str, reason: str, ctx: Context, hold: bool = False, deciding_test: str = "other") -> dict:
    """Optional: record the final call on a star after working through the procedure. verdict is one of
    candidate, eclipsing_binary, blend_or_off_target, systematics_or_noise, known_object, undecided. reason: one or two sentences citing the numbers.
    hold=true means "keep it, but it is not ready to report" (grazing transit, diluted depth, needs a mass). deciding_test names the test that
    settled it: centroid, secondary, odd_even, duration, recovery, catalog, dilution, snr or other.
    Verdicts are logged anonymously with the other tool calls; nothing is published or submitted anywhere."""
    if verdict not in VERDICTS:
        raise ToolError(f"verdict must be one of {', '.join(VERDICTS)}")
    if deciding_test not in DECIDING:
        raise ToolError(f"deciding_test must be one of {', '.join(DECIDING)}")
    sid = store.session_id(dict(ctx.headers) if ctx.headers else {})
    out = {"recorded": True, "tic": int(tic), "verdict": verdict, "hold": bool(hold)}
    store.trace(sid, "report_verdict", {"tic": int(tic), "verdict": verdict, "hold": bool(hold), "deciding_test": deciding_test,
                                        "reason": reason[:600], "tools_seen": store.seen(sid, tic)}, out, 0, False)
    return out


def _guide():
    return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide.md")).read()


@mcp.resource("tess-hunt://guide", name="guide", title="Vetting guide", mime_type="text/markdown",
              description="How transit signals fool you: the common impostors, the test that catches each, and this server's limits.")
def guide() -> str:
    return _guide()


@mcp.tool(title="Read the vetting guide")
async def read_guide(ctx: Context) -> str:
    """How transit signals fool you: common impostors, the test that catches each, how to read the numbers, and this server's limits.
    Background only: it does not replace running the tools on the star you were asked about."""
    return _guide()


@mcp.prompt(title="Hunt: vet one star end to end")
def hunt(star: str) -> str:
    """The fixed per-star procedure, with the language rules."""
    return f"""Check the star "{star}" for a transiting planet candidate using the tess-hunt tools. Work in this order and show the numbers at each step.

1. read_guide (or the tess-hunt://guide resource) for the impostors to rule out. It is background: every number in your answer must come from a tool run in this conversation.
2. resolve_star. Note the radius, magnitude, contamination ratio and observed sectors.
3. catalog_check. If it is a known planet host or TOI, say so: anything you find is a recovery, not something new.
4. search_transits on the earliest observed sector. If SNR is under 8 or the bar fails for shape reasons, try one more sector (a later one has finer time sampling), then stop and report "no convincing signal".
5. vet_signal with the period, t0 and duration from the search. Look at the phase-curve panel yourself for a second dip.
6. neighbors with the measured depth. Name any star that could fake the dip.
7. followup_start with the same signal, then job_status until done. The signal must repeat in at least max(2, usable-1) sectors.
8. Verdict, one of: candidate, eclipsing_binary, blend_or_off_target, systematics_or_noise, known_object, undecided. Give the two or three numbers that decided it and what observation would settle it. Optionally call report_verdict.

Language: say "planet candidate", never "planet" or "discovered". A tool error is not evidence about the star; retry or say the step could not be done."""


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True, "server": "tess-hunt", "version": "0.1.0"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true", help="run over stdio for a local client")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = ap.parse_args()
    if args.stdio:
        mcp.run("stdio")
        return
    hosts = [h.strip() for h in os.environ.get("TESSHUNT_MCP_HOSTS", "").split(",") if h.strip()]
    if "*" in hosts:
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        local = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        sec = TransportSecuritySettings(allowed_hosts=local + hosts + [h + ":*" for h in hosts],
                                        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + [f"https://{h}" for h in hosts])
    mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=True, transport_security=sec)


if __name__ == "__main__":
    main()
