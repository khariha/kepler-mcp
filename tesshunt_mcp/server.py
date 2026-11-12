"""Kepler MCP server.

    tesshunt-mcp                 # streamable HTTP on 0.0.0.0:8000/mcp (hosted)
    tesshunt-mcp --stdio         # local, for Claude Code / Claude Desktop

Env: TESSHUNT_MCP_DATA (cache + traces dir), TESSHUNT_MCP_HOSTS (comma-separated allowed Host headers, or *),
     TESSHUNT_MCP_LIGHT_PER_HOUR, TESSHUNT_MCP_HEAVY_PER_HOUR, TESSHUNT_MCP_TIMEOUT_S, TESSHUNT_MCP_CONCURRENCY,
     TESSHUNT_MCP_TRACES=0 to turn the trace log off, TESSHUNT_MCP_SALT.
"""
import argparse, os, re, sys, time, warnings
import anyio
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from tesshunt_mcp import core, leads, sky, store

warnings.filterwarnings("ignore")
# Every tool only reads public archives and computes; nothing is created, changed or sent on the caller's behalf.
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
NOTE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
TIMEOUT = float(os.environ.get("TESSHUNT_MCP_TIMEOUT_S", 120))
_slots = None   # created inside the event loop

INSTRUCTIONS = """Kepler: hunt for transiting planet CANDIDATES in NASA TESS data, with a guide at your side. (Named for Johannes Kepler; the data is from the TESS mission, not the Kepler space telescope.)

First contact: when the user arrives, greets you, or says something like "let's start" or "let's hunt", call `welcome` and offer its two paths in your own words: Learn (find a known planet from raw data, explained step by step; call `lesson`) or Hunt (take a real unchecked lead, try to break it, get credited on the public board; call `join` then `next_lead`, which can filter by star colour or planet size when the user has a preference; when no lead is waiting, `open_patch` lets the user's own computer search a fresh patch of sky). If the user names a specific star instead, check that star with the `check_star` procedure.

Hand-holding: most results carry `next` (the sensible next step, with exact values to pass) and `plain` (what the result means in one sentence). Follow `next` unless the user asks for something else, and explain each step in plain words before showing numbers. Ask the user a question now and then; this is their hunt.

Rules for whoever reads the results:
- Say "planet candidate", never "planet" or "discovered". Nothing here confirms a planet; only follow-up observations can.
- A single sector is never enough. A signal must repeat (followup_start) and show no second eclipse before it is called a candidate.
- Every number you report about a star must come from a tool run on that star in this conversation. The vetting guide (read_guide) is background, not evidence.
- A network or archive failure is an error to retry, not a verdict about the star.
- Be honest about odds: most leads are impostors, and most stars show nothing.

Times are BTJD (BJD - 2457000). Depths are ppm. Calls are rate limited per user; cached answers are free.
Privacy: tool names, arguments and numeric results are logged with an anonymous daily-rotating id to improve the tools; the log may be used to train open models. No IP address, account, prompt or model reasoning is stored. A planet-hunter name, if the user picks one, is shown on the public board.

Vocabulary: call the user a "planet hunter". What they find is always a "planet candidate"."""

mcp = MCPServer("kepler", title="Kepler: hunt for planets in NASA TESS data", instructions=INSTRUCTIONS,
                website_url="https://github.com/khariha/kepler-mcp", version="0.2.0")


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


@mcp.tool(title="Resolve a star", annotations=READ)
async def resolve_star(query: str, ctx: Context) -> list:
    """Find a star in the TESS Input Catalog. query: 'TIC 261136679', a bare TIC number, a name ('Pi Mensae', 'TOI-700', 'LHS 3844'), or 'ra dec' in degrees.
    Returns TIC id, coordinates, TESS magnitude, temperature, radius, mass, distance, contamination ratio, and the sectors that observed it."""
    q = query.strip()[:80]
    return await _run("resolve_star_tool", "light", ctx, {"q": q.lower()}, lambda: core.resolve_star(q))


@mcp.tool(title="Check catalogs", annotations=READ)
async def catalog_check(tic: int, ctx: Context) -> list:
    """Is this TIC already a confirmed planet host, a TESS Object of Interest (TOI), or a Community TOI? Do this before any search:
    a signal on a known planet host is a recovery, not a new candidate."""
    return await _run("catalog_check", "light", ctx, {"tic": tic}, lambda: core.catalog_check(tic))


@mcp.tool(title="Light curve from full-frame images", annotations=READ)
async def light_curve(tic: int, sector: int, ctx: Context) -> list:
    """Build the star's brightness-vs-time curve for one sector from raw TESS full-frame-image pixels (first call per star and sector takes 5-50 s).
    Returns cadence, noise, a binned series and a plot. Use a sector from resolve_star."""
    return await _run("light_curve", "light", ctx, {"tic": tic, "sector": sector}, lambda: core.light_curve(tic, sector))


@mcp.tool(title="Search for transits", annotations=READ)
async def search_transits(tic: int, sector: int, ctx: Context, min_period_d: float = 0.3, max_period_d: float = 13.0,
                          min_snr: float = 9.0, min_depth_ppm: float = 0.0) -> list:
    """Box-least-squares search of one sector for a periodic dip. Returns the strongest signal (period, epoch t0, duration, depth, SNR),
    quick shape tests, whether it passes the candidate bar and why not, plus a periodogram and fold plot. Every star returns a 'best' peak;
    most are noise, so read SNR, depth_sigma and failed_checks. Narrow the period range to chase a specific signal; raise min_depth_ppm
    (for example 3000) to mimic a survey that only wants deep dips."""
    a = {"tic": tic, "sector": sector, "min_period_d": min_period_d, "max_period_d": max_period_d, "min_snr": min_snr, "min_depth_ppm": min_depth_ppm}
    return await _run("search_transits", "light", ctx, a, lambda: core.search_transits(tic, sector, min_period_d, max_period_d, min_snr, min_depth_ppm))


@mcp.tool(title="Vet a signal", annotations=READ)
async def vet_signal(tic: int, sector: int, period_d: float, t0: float, duration_h: float, ctx: Context) -> list:
    """Try to break a signal in one sector: odd vs even depths, a secondary eclipse at ANY orbital phase (catches eccentric eclipsing binaries),
    duration against the physical maximum, centroid shift, difference image, and Gaia neighbours. Returns a verdict with reasons and a three-panel plot.
    t0 is a mid-transit time in BTJD, as returned by search_transits."""
    a = {"tic": tic, "sector": sector, "period_d": period_d, "t0": t0, "duration_h": duration_h}
    return await _run("vet_signal", "light", ctx, a, lambda: core.vet_signal(tic, sector, period_d, t0, duration_h))


@mcp.tool(title="Gaia neighbours", annotations=READ)
async def neighbors(tic: int, sector: int, ctx: Context, depth_ppm: float | None = None) -> list:
    """Gaia DR3 stars within 3 TESS pixels (63 arcsec) of the target, with separation and brightness relative to the target.
    Give depth_ppm to see how deep an eclipse on each neighbour would have to be to fake the dip."""
    a = {"tic": tic, "sector": sector, "depth_ppm": depth_ppm}
    return await _run("neighbors", "light", ctx, a, lambda: core.neighbors(tic, sector, depth_ppm))


@mcp.tool(title="Phase-folded plot", annotations=READ)
async def fold_plot(tic: int, sectors: list[int], period_d: float, t0: float, ctx: Context) -> list:
    """Fold up to 4 sectors at a period and epoch and overlay them. Useful to see whether the dip lines up across sectors."""
    a = {"tic": tic, "sectors": sorted(set(sectors))[:4], "period_d": period_d, "t0": t0}
    return await _run("fold_plot", "light", ctx, a, lambda: core.fold_plot(tic, a["sectors"], period_d, t0))


def _followup_args(tic, period_d, t0, duration_h, depth_ppm):
    return {"tic": int(tic), "period_d": period_d, "t0": t0, "duration_h": duration_h, "depth_ppm": depth_ppm}


@mcp.tool(title="Multi-sector follow-up (analysis only)", annotations=READ)
async def followup_start(tic: int, period_d: float, t0: float, duration_h: float, depth_ppm: float, ctx: Context) -> dict:
    """The decisive test, read-only: analyse every sector that observed the star for the same signal, then refine the period on the stitched
    baseline. It only reads public data and computes; nothing is created or changed anywhere. The analysis takes 1-5 minutes, so this
    returns a job_id at once; call job_status(job_id), which waits and reports progress, until it says done."""
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


@mcp.tool(title="Follow-up job status", annotations=READ)
async def job_status(job_id: str, ctx: Context, wait_s: float = 20.0) -> list:
    """Result of a follow-up job. Waits up to wait_s seconds (default 20, max 25) for it to finish, sending live progress
    (which sector is being analysed) while it waits, so one or two calls are usually enough. Returns queued or running with
    the latest progress line, failed with the reason, or done with the full result and the stitched fold plot."""
    if not re.fullmatch(r"[0-9a-f]{20}", job_id):
        raise ToolError("job_id is the 20-character id returned by followup_start")
    sid = store.session_id(dict(ctx.headers) if ctx.headers else {})
    deadline = time.time() + max(0.0, min(float(wait_s), 25.0))
    last = None
    while True:
        state, hit = store.job_get("followup", job_id)
        if hit is not None or state["state"] in ("failed", "unknown") or time.time() >= deadline:
            break
        if state.get("progress") and state["progress"] != last:
            last = state["progress"]
            try:
                await ctx.report_progress(float(state.get("steps_done", 0)), None, last)
            except Exception:
                pass   # the client did not ask for progress, or the transport cannot stream it
        await anyio.sleep(1.0)
    if hit is None:
        return [state]
    result, png = hit
    store.trace(sid, "job_status", {"job_id": job_id}, result, 0, True)
    return _content({"state": "done", **result}, png)


VERDICTS = ("candidate", "eclipsing_binary", "blend_or_off_target", "systematics_or_noise", "known_object", "undecided")
DECIDING = ("centroid", "secondary", "odd_even", "duration", "recovery", "catalog", "dilution", "snr", "other")


@mcp.tool(title="Record a verdict", annotations=NOTE)
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


# ---------------- the leads queue ----------------
def _sid(ctx):
    return store.session_id(dict(ctx.headers) if (ctx is not None and ctx.headers) else {})


def _lead_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except leads.LeadError as e:
        raise ToolError(str(e)) from None


@mcp.tool(title="Join the hunt", annotations=NOTE)
async def join(handle: str, ctx: Context) -> dict:
    """Register a planet-hunter name (3-20 letters, digits, dash or underscore) to take leads and be credited on the public board.
    Returns a hunter_id: tell the user to keep it, because it is how they continue as the same hunter in a later session."""
    out = _lead_call(leads.join, handle)
    store.trace(_sid(ctx), "join", {"handle": out["handle"]}, {"joined": True}, 0, False)
    return out | {"plain": f"You are now a planet hunter, signed in as {out['handle']}. Keep your hunter_id; it is how you pick up where you left off.",
                  "next": f"next_lead(hunter_id='{out['hunter_id']}')"}


@mcp.tool(title="Get a lead", annotations=NOTE)
async def next_lead(hunter_id: str, ctx: Context, star_type: str | None = None, planet_size: str | None = None) -> dict:
    """Take the next unchecked lead: a star where the bulk search saw a repeating dip. Returns the star, the signal, and the exact values to
    pass to the tests. The lead is held for you for 30 minutes.
    Optional filters, for a hunter who wants a particular kind: star_type = red, orange, yellow or white (yellow is Sun-like);
    planet_size = rocky (under 1.8 Earth radii), sub_neptune, neptune or giant, judged from the dip's depth if it were a planet.
    When nothing matches, the reply says what is available instead."""
    if star_type and star_type not in leads.STAR_TYPES:
        raise ToolError(f"star_type must be one of {', '.join(leads.STAR_TYPES)}")
    if planet_size and planet_size not in leads.PLANET_SIZES:
        raise ToolError(f"planet_size must be one of {', '.join(leads.PLANET_SIZES)}")
    lead = _lead_call(leads.claim_next, hunter_id, None, star_type, planet_size)
    if lead is None:
        avail = _lead_call(leads.available, hunter_id)
        if avail["open_for_you"] and (star_type or planet_size):
            return {"lead": None, "available": avail,
                    "plain": "No open lead matches that filter. Here is what is in the queue for you; pick from these or drop the filter.",
                    "next": "tell the user what kinds are available and call next_lead again with a filter that has leads, or with none"}
        return {"lead": None, "available": avail,
                "plain": "There are no open leads you have not already vetted. New leads come from searching new sky, and you can do that yourself: "
                         "open a patch of about 150 unsearched stars and your computer searches it in about seven minutes. "
                         "Patches come in three star types: red (best odds), orange, and yellow (Sun-like, giant planets only).",
                "next": f"offer to open a patch: open_patch(hunter_id='{hunter_id}', star_type='red' | 'orange' | 'yellow'); or sky_map() to show coverage first"}
    star = lead["star"]
    desc = []
    if star.get("Tmag") is not None:
        desc.append(f"TESS magnitude {star['Tmag']:.1f}")
    if star.get("rad"):
        desc.append(f"{star['rad']:.2f} times the Sun's radius")
    if star.get("d"):
        desc.append(f"{star['d'] * 3.26:.0f} light-years away")
    vals = f"period_d={lead['period_d']:.6f}, t0={lead['t0']:.5f}, duration_h={lead['duration_h']:.2f}"
    if lead.get("star_type"):
        desc.insert(0, f"a {lead['star_type']} star")
    size = (f" If it is a planet it would be about {lead['radius_earth_if_planet']:.1f} times Earth's radius ({lead['size_if_planet'].replace('_', '-')})."
            if lead.get("radius_earth_if_planet") else "")
    lead["brief"] = (f"Lead {lead['lead_id']}: TIC {lead['tic']}" + (f" ({', '.join(desc)})" if desc else "") +
                     f". In sector {lead['sector']} the automatic search saw a dip of {lead['depth_ppm']/1e4:.2f}% every {lead['period_d']:.3f} days.{size} " +
                     ("PRACTICE LEAD: a well-known star kept for practice, so expect a known planet or a known impostor. Tell the user so. "
                      if lead.get("practice") else
                      (f"It came from a patch of sky searched by {lead['found_by']}. " if lead.get("found_by") else "") + "Nobody has checked whether it is real. ")
                     + "Your job is to try to break it.")
    lead["odds"] = "Most leads turn out to be eclipsing binaries, spotted stars or a neighbour's eclipse. Roughly 1 in 10 survives."
    lead["available"] = _lead_call(leads.available, hunter_id)
    lead["next"] = (f"1) catalog_check(tic={lead['tic']})  2) vet_signal(tic={lead['tic']}, sector={lead['sector']}, {vals})  "
                    f"3) neighbors(tic={lead['tic']}, sector={lead['sector']}, depth_ppm={lead['depth_ppm']:.0f})  "
                    f"4) followup_start(tic={lead['tic']}, {vals}, depth_ppm={lead['depth_ppm']:.0f}) then job_status until done  "
                    f"5) submit_vet(hunter_id, lead_id={lead['lead_id']}, verdict, reason, deciding_test). Use these exact values so your result can be checked.")
    store.trace(_sid(ctx), "next_lead", {"tic": lead["tic"], "lead_id": lead["lead_id"]}, {"tic": lead["tic"], "lead_id": lead["lead_id"]}, 0, False)
    return lead


def _auto_status(lead):
    """The server's own reading of a lead, from its cached results for exactly the lead's values. None if the tests have not been run."""
    a = {"tic": lead["tic"], "sector": lead["sector"], "period_d": lead["period_d"], "t0": lead["t0"], "duration_h": lead["duration_h"]}
    vet = store.cache_get("vet_signal", store.key("vet_signal", a))
    fol = store.cache_get("followup", store.key("followup", _followup_args(lead["tic"], lead["period_d"], lead["t0"], lead["duration_h"], lead["depth_ppm"])))
    if vet is None or fol is None:
        return None, vet is not None, fol is not None
    v, f = vet[0], fol[0]
    cat = store.cache_get("catalog_check", store.key("catalog_check", {"tic": lead["tic"]}))
    if cat is not None and not lead.get("practice") and (cat[0]["known_planet_host"] or cat[0]["is_toi"] or cat[0]["is_ctoi"]):
        return "known_object", True, True     # it became a listed object after the lead was made: not ours to claim
    if f["status"] == "eclipsing_binary" or v["verdict"] == "eclipsing_binary":
        return "eclipsing_binary", True, True
    if f["status"] == "repeats" and v["verdict"] in ("on_target", "likely_on_target"):
        return "candidate_like", True, True
    return (f["status"] if f["status"] != "repeats" else v["verdict"]), True, True


@mcp.tool(title="Submit your verdict on a lead", annotations=NOTE)
async def submit_vet(hunter_id: str, lead_id: int, verdict: str, reason: str, ctx: Context, deciding_test: str = "other", hold: bool = False) -> dict:
    """Record your verdict on a lead after running the tests on it. verdict: candidate, eclipsing_binary, blend_or_off_target,
    systematics_or_noise, known_object or undecided. reason: one or two sentences citing the numbers. deciding_test: centroid, secondary,
    odd_even, duration, recovery, catalog, dilution, snr or other. hold=true means it looks real but is not ready to stand as a candidate.
    Refused until vet_signal and a finished follow-up have been run with the lead's exact values. A lead joins the public board only when
    two different hunters call it a candidate and the server's own results agree."""
    if verdict not in VERDICTS:
        raise ToolError(f"verdict must be one of {', '.join(VERDICTS)}")
    if deciding_test not in DECIDING:
        raise ToolError(f"deciding_test must be one of {', '.join(DECIDING)}")
    lead = _lead_call(leads.get, lead_id)
    auto, has_vet, has_follow = _auto_status(lead)
    if auto is None:
        vals = f"period_d={lead['period_d']:.6f}, t0={lead['t0']:.5f}, duration_h={lead['duration_h']:.2f}"
        missing = []
        if not has_vet:
            missing.append(f"vet_signal(tic={lead['tic']}, sector={lead['sector']}, {vals})")
        if not has_follow:
            missing.append(f"followup_start(tic={lead['tic']}, {vals}, depth_ppm={lead['depth_ppm']:.0f}) and job_status until it says done")
        raise ToolError("a verdict needs the evidence behind it. Still to run, with these exact values: " + "; then ".join(missing))
    sid = _sid(ctx)
    status, info = _lead_call(leads.record, hunter_id, lead_id, verdict, hold, deciding_test, reason, auto, store.seen(sid, lead["tic"]))
    me = leads.stats(hunter_id); bd = leads.board(limit=1)
    if status == "surviving":
        plain = f"Lead {lead_id} has survived two independent vets and the server's own tests. It is now on the public board as an unconfirmed planet candidate."
    elif status == "cleared":
        plain = f"Lead {lead_id} is cleared: not a planet candidate. Clearing impostors is most of the work, and this one is now off everyone's list."
    elif verdict == "candidate" and info["server_agrees_candidate"]:
        plain = f"Your vote is in and the server's tests agree. Lead {lead_id} now waits for a second planet hunter to vet it independently before it can go on the board."
    elif verdict == "candidate":
        plain = f"Your vote is recorded, but the server's own results read this lead as '{auto}', so it cannot go on the board. Another planet hunter will take a look."
    else:
        plain = f"Recorded. The server's own results read this lead as '{auto}'; another planet hunter will take a second look."
    out = {"lead_id": int(lead_id), "lead_status": status, "server_reading": auto, **info, "plain": plain,
           "you": me, "board": {k: bd[k] for k in ("leads_total", "leads_open", "leads_cleared", "leads_surviving")},
           "next": f"next_lead(hunter_id='{hunter_id}') for another, or board() to see the standings"}
    store.trace(sid, "submit_vet", {"tic": lead["tic"], "lead_id": int(lead_id), "verdict": verdict, "hold": bool(hold), "deciding_test": deciding_test,
                                    "reason": reason[:600], "auto_status": auto, "tools_seen": store.seen(sid, lead["tic"])},
                {"lead_status": status}, 0, False)
    return out


@mcp.tool(title="The board", annotations=READ)
async def board(ctx: Context) -> dict:
    """Public standings: how many leads are open, cleared and surviving, the surviving candidates with the hunters who vetted them,
    leads waiting for a second vet, and the most active hunters."""
    return leads.board()


@mcp.tool(title="My progress", annotations=READ)
async def my_progress(hunter_id: str, ctx: Context) -> dict:
    """How many leads this hunter has vetted, their verdicts, and how many of their candidates are on the board."""
    return _lead_call(leads.stats, hunter_id)


@mcp.prompt(title="Hunt: vet real leads")
def hunt() -> str:
    """The guided hunting loop over the leads queue."""
    return """Let's hunt with Kepler. Guide me; assume I am new to this.

1. Ask me for a planet-hunter name and call join. Tell me to keep the hunter_id.
2. Call next_lead. If I asked for a kind of star or planet (a Sun-like star, a rocky planet), pass star_type or planet_size; if none matches, tell me what is
   available. Read me the brief in your own words: which star, what the search saw, and that most leads are impostors.
3. Work through the lead's `next` steps one at a time, with the exact values given. Before each test say in a sentence what impostor it is looking for;
   after it, say what the result means (`plain`) and show the two or three numbers that matter. Show me the plots.
4. When the tests are done, tell me your verdict and why, ask whether I agree, then call submit_vet.
5. Tell me what happened to the lead and where I stand, then ask whether I want another. Stop after five leads unless I ask for more.
6. If no lead is waiting, offer to open a patch of sky (open_patch). Explain that my computer will search about 150 unsearched stars in about seven minutes
   and that most patches find nothing. Run the command if you can, or give it to me to paste. Then patch_status, and vet whatever it found.

Say "planet candidate", never "planet" or "discovered". Every number must come from a tool run in this conversation. If a tool fails, retry once, then say the step could not be done."""


# ---------------- the sky map: patches searched on hunters' own computers ----------------
EXCLUDE_FILE = os.environ.get("KEPLER_EXCLUDE", os.path.join(store.ROOT, "ops", "private_exclude.txt"))
SWEEP_CMD = os.environ.get("KEPLER_SWEEP_CMD", "kepler-sweep")
PUBLIC_URL = os.environ.get("KEPLER_PUBLIC_URL", "")


def _sky_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except (sky.SkyError, leads.LeadError) as e:
        raise ToolError(str(e)) from None


def _map_detector(sector, camera, ccd, pop="red"):
    """Build one detector's patches for one star type in the background (several minutes of catalogue queries)."""
    if sky.ccd_status(sector, camera, ccd, pop) in ("building", "ready"):
        return False
    sky.mark_ccd(sector, camera, ccd, "building", pop)

    def work():
        try:
            from tesshunt.targets import select_detector
            lim = sky.POPULATIONS[pop]
            exclude = set(open(EXCLUDE_FILE).read().split()) if os.path.exists(EXCLUDE_FILE) else set()
            stars = select_detector(sector, camera, ccd, os.path.join(store.DATA, "catalog"), exclude=exclude,
                                    tmag=lim["tmag"], teff=lim["teff"], rad_max=lim["rad_max"])
            sky.build_ccd(sector, camera, ccd, stars, pop)
        except Exception as e:
            sky.mark_ccd(sector, camera, ccd, f"failed: {type(e).__name__}: {str(e)[:120]}", pop)
    store._pool.submit(work)
    return True


def _map_next(pop="red"):
    """Start mapping the first detector of the open sectors not yet mapped for this star type. Returns (sector, camera, ccd) or None."""
    for sec in sky.OPEN_SECTORS:
        for cam in (1, 2, 3, 4):
            for ccd in (1, 2, 3, 4):
                st = sky.ccd_status(sec, cam, ccd, pop)
                if st == "building":
                    return (sec, cam, ccd)
                if st is None or str(st).startswith("failed"):
                    _map_detector(sec, cam, ccd, pop)
                    return (sec, cam, ccd)
    return None


def _base_url(ctx):
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    h = dict(ctx.headers) if (ctx is not None and ctx.headers) else {}
    host = h.get("x-forwarded-host") or h.get("host") or "127.0.0.1:8000"
    scheme = h.get("x-forwarded-proto") or ("http" if host.split(":")[0] in ("127.0.0.1", "localhost") else "https")
    return f"{scheme}://{host}"


@mcp.tool(title="The sky map", annotations=READ)
async def sky_map(ctx: Context, sector: int | None = None) -> dict:
    """Which patches of sky have been searched and which are still unopened, per sector, detector and star type, with leads found so far.
    A patch is about 150 faint stars of one type (red, orange or yellow) that nobody has searched; opening one runs the search on the
    planet hunter's own computer (about seven minutes) and the result is kept for everyone. `star_types` says what each type can reveal."""
    out = sky.summary(sector)
    t = out["totals"]
    if t["unopened"] == 0 and not out["detectors_being_mapped"] and not sky.PAUSED:
        nxt = _map_next()
        if nxt:
            out["detectors_being_mapped"] = [{"sector": nxt[0], "camera": nxt[1], "ccd": nxt[2], "star_type": "red"}]
    if t["patches"] == 0:
        out["plain"] = ("No patches are mapped yet. Kepler is listing the stars on the first detector now, which takes about five minutes the first time. "
                        "Meanwhile there may be leads waiting (next_lead).")
        out["next"] = "tell the user the first patches are being prepared, offer a waiting lead, and call sky_map again in a few minutes"
    else:
        out["plain"] = (f"{t['searched']} of {t['patches']} mapped patches have been searched ({t['stars_searched']} stars), finding {t['leads_found']} lead(s). "
                        f"{t['unopened']} patch(es) are unopened and ready.")
        out["next"] = "offer the user a waiting lead (next_lead) or a fresh patch of sky (open_patch)"
    return out


@mcp.tool(title="Open a patch of sky", annotations=NOTE)
async def open_patch(hunter_id: str, ctx: Context, patch_id: str | None = None, star_type: str | None = None) -> dict:
    """Claim an unsearched patch of sky (about 150 stars) for this planet hunter and get the one command that searches it on their own computer.
    star_type picks the kind of star: red (default; small red dwarfs, best odds), orange, or yellow (faint Sun-like stars, where only giant
    planets can show). The search takes about seven minutes, reads NASA's public data directly, and uploads only the results. The patch is
    held for two hours. If you can run shell commands, run the returned command for the user and relay its progress; otherwise ask them to
    paste it into a terminal. If no patch of that type is mapped yet, this starts the mapping (several minutes) and says so."""
    if star_type and star_type not in sky.POPULATIONS:
        raise ToolError(f"star_type must be one of {', '.join(sky.POPULATIONS)}; white and hotter stars are not offered")
    pop = star_type or (None if patch_id else "red")
    p = _sky_call(sky.claim, hunter_id, patch_id, None, pop)
    if p is None:
        nxt = None if sky.PAUSED else _map_next(pop or "red")
        kind = pop or "red"
        return {"patch": None, "star_type": kind,
                "plain": (f"No {kind}-star patch is ready yet. Kepler is listing the unsearched {kind} stars on the next detector now; "
                          f"patches appear in about {'ten to twenty' if kind == 'yellow' else 'five to ten'} minutes. " + sky.POPULATIONS[kind]["finds"]
                          if nxt else "There are no unopened patches right now."),
                "ready_now": sky.summary()["unopened_by_star_type"],
                "next": "offer a patch of a star type that is ready now, or a waiting lead with next_lead, and try this again in a few minutes"}
    cmd = f"{SWEEP_CMD} {p['id']} --hunter {hunter_id} --hub {_base_url(ctx)}"
    store.trace(_sid(ctx), "open_patch", {"patch_id": p["id"]}, {"patch_id": p["id"]}, 0, False)
    return {"patch_id": p["id"], "sector": p["sector"], "star_type": p["pop"], "stars": p["n_stars"], "held_until_minutes": int((p["claimed_until"] - time.time()) / 60),
            "command": cmd, "resumed": p["resumed"], "what_this_can_find": sky.POPULATIONS[p["pop"]]["finds"],
            "plain": (f"Patch {p['id']} is yours: {p['n_stars']} faint {p['pop']} stars in sector {p['sector']} that nobody has searched. "
                      "The search runs on your computer and takes about seven minutes. Many patches turn up nothing; when one finds a signal it becomes a lead under your name."),
            "first_time": "The first run installs the science libraries, which takes a few minutes once. It needs Python 3.11 or newer.",
            "next": f"run the command (or ask the user to), then call patch_status(patch_id='{p['id']}') to see what came back"}


@mcp.tool(title="Patch status", annotations=READ)
async def patch_status(patch_id: str, ctx: Context, wait_s: float = 20.0) -> dict:
    """What happened to a patch: unopened, claimed (search not uploaded yet), verifying (the hub is re-checking the signals), or done with the
    leads it produced. Waits up to wait_s seconds (max 25) while the hub is verifying."""
    deadline = time.time() + max(0.0, min(float(wait_s), 25.0))
    while True:
        p = _sky_call(sky.get, patch_id)
        if p["status"] != "verifying" or time.time() >= deadline:
            break
        await anyio.sleep(1.0)
    out = {"patch_id": patch_id, "status": p["status"], "stars": p["n_stars"], "opened_by": p["opened_by"], "signals_flagged": p["n_flagged"], "leads": p["n_leads"]}
    if p["status"] == "done":
        out["plain"] = (f"Patch searched and stored. {p['n_leads']} signal(s) passed the hub's own re-check and joined the leads queue, credited to {p['opened_by']}."
                        if p["n_leads"] else "Patch searched and stored. Nothing in it passed the checks, which is the usual result; that sky is now cleared for everyone.")
        out["next"] = "next_lead to vet what was found" if p["n_leads"] else "open another patch with open_patch, or take a waiting lead"
    elif p["status"] == "verifying":
        out["plain"] = f"Results received. The hub is re-running {p['n_flagged']} flagged star(s) itself before they become leads."
        out["next"] = "call patch_status again"
    elif p["status"] == "claimed":
        out["plain"] = "The patch is held but no results have arrived yet. The search command is still running, or has not been started."
        out["next"] = "wait for the search command to finish, then call patch_status again"
    else:
        out["plain"] = "This patch is unopened." + (f" (Last attempt was rejected: {p['note']})" if p.get("note") else "")
    return out


def _verify_patch(pid, hunter_handle, flagged, quiet_tic):
    """The hub's own check of an upload: re-run every flagged star, plus one quiet one. Runs in the job pool."""
    def as_row(res):
        b = res["best"]
        return {"status": "ok", "period_d": b["period_d"], "t0": b["t0"], "duration_h": b["duration_h"], "max_duration_h": b["max_physical_duration_h"],
                "depth_ppm": b["depth_ppm"], "depth_err_ppm": b["depth_err_ppm"], "snr": b["snr"], "n_transits": b["n_transits"], "cadence_min": None}
    try:
        p = sky.get(pid)
        sector = p["sector"]
        if quiet_tic is not None:
            try:
                res, _ = core.search_transits(quiet_tic, sector)
                if sky.lead_bar(as_row(res)) and res["best"]["snr"] >= 12:
                    sky.finish(pid, [], rejected_reason=f"the upload called TIC {quiet_tic} quiet but the hub finds a strong signal there")
                    return
            except Exception:
                pass      # an archive hiccup on the spot check is not evidence against the upload
        verified = []
        for r in flagged[:40]:
            try:
                res, _ = core.search_transits(r["tic"], sector)
            except Exception:
                continue
            mine = as_row(res)
            ratio = mine["period_d"] / r["period_d"] if r["period_d"] else 0
            same = any(abs(ratio - k) < 0.02 * k for k in (0.5, 1.0, 2.0))
            if same and sky.lead_bar(mine):
                star = sky.star_of(pid, r["tic"]) or {}
                lead = {"tic": r["tic"], "sector": sector, "period_d": mine["period_d"], "t0": mine["t0"], "duration_h": mine["duration_h"],
                        "depth_ppm": mine["depth_ppm"], "snr": mine["snr"], "star": {k: star.get(k) for k in ("Tmag", "Teff", "rad", "mass", "d", "contratio")}}
                if leads.add(lead, found_by=hunter_handle, patch_id=pid):
                    verified.append(lead)
        sky.finish(pid, verified)
    except Exception as e:
        sky.finish(pid, [], rejected_reason=f"the hub could not verify the upload ({type(e).__name__}); please run the search again later")


def _guide():
    return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide.md")).read()


@mcp.resource("kepler://guide", name="guide", title="Vetting guide", mime_type="text/markdown",
              description="How transit signals fool you: the common impostors, the test that catches each, and this server's limits.")
def guide() -> str:
    return _guide()


@mcp.tool(title="Read the vetting guide", annotations=READ)
async def read_guide(ctx: Context) -> str:
    """How transit signals fool you: common impostors, the test that catches each, how to read the numbers, and this server's limits.
    Background only: it does not replace running the tools on the star you were asked about."""
    return _guide()


WELCOME = {
    "greeting": "Welcome to Kepler, planet hunter. Let's hunt for planets.",
    "about": "Kepler searches NASA's TESS space-telescope data for planets crossing in front of their stars, then tries hard to prove each signal wrong. "
             "What survives is a planet candidate. (Named for Johannes Kepler; the data is from TESS.)",
    "paths": [
        {"name": "Learn", "time": "about 10 minutes",
         "what": "Find a planet that astronomers already know about, starting from raw telescope pixels, with every step explained.",
         "how": "call lesson"},
        {"name": "Hunt", "time": "5 to 10 minutes per lead",
         "what": "Take a real lead that nobody has checked yet: a star where a search saw a repeating dip. Try to break it. "
                 "If it survives your tests and a second planet hunter's, it goes on the public board with your name. "
                 "When the queue is empty you can open a fresh patch of sky: your own computer searches about 150 stars nobody has looked at, in about seven minutes.",
         "odds": "Most leads are impostors: eclipsing binaries, spotted stars, a neighbour's eclipse. Expect roughly 1 in 10 to survive.",
         "how": "ask the user for a planet-hunter name, call join, then next_lead; if no lead is waiting, sky_map and open_patch"},
        {"name": "Bring your own star", "time": "a few minutes",
         "what": "Name any star TESS observed and run the full check on it.", "how": "follow the check_star procedure"},
    ],
    "say": "Greet the user with the greeting, describe Learn and Hunt in a sentence each, mention they can also bring their own star, and ask which they want. Keep it short.",
}

LESSON = {
    "star": "Pi Mensae", "goal": "Recover the planet pi Mensae c from raw TESS full-frame images, and understand each test along the way.",
    "how_to_teach": "One step at a time. Before each tool call, say in one or two plain sentences what you are about to do and why. After it, show the key numbers and "
                    "what they mean. Then ask the step's question and wait for the answer before moving on. No jargon without a definition.",
    "steps": [
        {"n": 1, "title": "Meet the star", "do": "resolve_star('Pi Mensae'), then catalog_check",
         "explain": "Where the star is, how bright, and that TESS watched it in many month-long 'sectors'. The catalogue already lists a planet: we are checking we can find it ourselves.",
         "ask": "Before we look: if a planet crosses in front of a star, what do you expect to happen to the star's brightness?"},
        {"n": 2, "title": "See the light", "do": "light_curve for sector 1",
         "explain": "Each dot is one brightness measurement. The line looks flat because the dip we want is about 0.03%, far smaller than the scatter of single points.",
         "ask": "Can you spot a dip by eye? (Almost nobody can. That is why we need the next step.)"},
        {"n": 3, "title": "Find the rhythm", "do": "search_transits for sector 1",
         "explain": "The search tries thousands of periods and stacks the data at each. A real planet's dips line up at one period and stand out. Report period, depth, and SNR.",
         "ask": "The dip is about 260 parts per million. The star is a bit bigger than the Sun. How big do you think the planet is compared with Earth?"},
        {"n": 4, "title": "Try to break it", "do": "vet_signal with the values from `next`, then neighbors",
         "explain": "Impostors: two stars eclipsing each other (look for a second dip, or odd and even dips of different depth), a spotted star (dip too long), a neighbour's eclipse (dip not on our star). Go through each result.",
         "ask": "Which of those impostors worried you most, and did the numbers rule it out?"},
        {"n": 5, "title": "Does it come back?", "do": "followup_start with the values from `next`, then job_status until done",
         "explain": "One month proves little. A planet keeps its appointments: the same dip must appear in every sector. Show how many sectors agree and the refined period, then compare with the published 6.268 days.",
         "ask": "We just recovered a known planet. Ready to try a lead nobody has checked?"},
    ],
    "finish": "Say clearly that this was a recovery of a known planet, not a discovery. Then offer the Hunt path (join, next_lead).",
}


@mcp.tool(title="Welcome to Kepler", annotations=READ)
async def welcome(ctx: Context) -> dict:
    """Start here. Returns the greeting and the paths a newcomer can take (Learn, Hunt, or bring your own star). Call it when the user
    first arrives, greets you, or asks what they can do."""
    return WELCOME


@mcp.tool(title="Guided lesson: recover a known planet", annotations=READ)
async def lesson(ctx: Context) -> dict:
    """The Learn path: a five-step guided lesson that recovers a known planet (pi Mensae c) from raw data. Returns the steps, what to explain
    at each, and a question to ask the user. Run the real tools at each step; nothing is canned."""
    return LESSON


@mcp.prompt(title="Start: welcome to Kepler")
def start() -> str:
    """Greets the user and offers Learn or Hunt."""
    return "Call the Kepler `welcome` tool and greet me as it says. Offer me the paths and ask which I want."


@mcp.prompt(title="Check one star end to end")
def check_star(star: str) -> str:
    """The fixed per-star procedure, with the language rules."""
    return f"""Check the star "{star}" for a transiting planet candidate using the Kepler tools. Work in this order and show the numbers at each step.

1. read_guide (or the kepler://guide resource) for the impostors to rule out. It is background: every number in your answer must come from a tool run in this conversation.
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
    return JSONResponse({"ok": True, "server": "kepler", "version": "0.2.0"})


@mcp.custom_route("/patch/{pid}", methods=["GET"])
async def patch_get(request):
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(sky.stars_for(request.query_params.get("hunter", ""), request.path_params["pid"]))
    except (sky.SkyError, leads.LeadError) as e:
        return JSONResponse({"error": str(e)}, status_code=403)


@mcp.custom_route("/patch/{pid}/status", methods=["GET"])
async def patch_state(request):
    from starlette.responses import JSONResponse
    try:
        p = sky.get(request.path_params["pid"])
    except sky.SkyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({k: p[k] for k in ("id", "status", "n_stars", "n_flagged", "n_leads", "opened_by", "note")})


@mcp.custom_route("/patch/{pid}/results", methods=["POST"])
async def patch_post(request):
    from starlette.responses import JSONResponse
    body = await request.body()
    if len(body) > 2_000_000:
        return JSONResponse({"error": "upload too large"}, status_code=413)
    try:
        import json as _json
        data = _json.loads(body)
        pid, hunter = request.path_params["pid"], str(data.get("hunter", ""))
        flagged, quiet = sky.submit(hunter, pid, data.get("rows"))
        handle = sky.get(pid)["opened_by"]
    except (sky.SkyError, leads.LeadError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "the upload could not be read"}, status_code=400)
    store._pool.submit(_verify_patch, pid, handle, flagged, quiet)
    return JSONResponse({"received": True, "status": "verifying", "signals_flagged": len(flagged),
                         "message": f"Results received. The hub is re-checking {len(flagged)} flagged star(s) before they become leads."})


@mcp.custom_route("/board.json", methods=["GET"])
async def board_json(request):
    from starlette.responses import JSONResponse
    return JSONResponse(leads.board() | {"sky": sky.summary()})


@mcp.custom_route("/board", methods=["GET"])
async def board_html(request):
    import html
    from starlette.responses import HTMLResponse
    b = leads.board()
    e = html.escape

    def rows(items, who_key):
        if not items:
            return '<tr><td colspan="4" class="none">None yet.</td></tr>'
        return "".join(f"<tr><td>TIC {i['tic']}</td><td>{i['period_d']:.3f} d</td><td>{i['depth_ppm']/1e4:.2f}%</td><td>{e(', '.join(i[who_key]))}</td></tr>" for i in items)
    sk = sky.summary(); st = sk["totals"]
    cover = "".join(f"<tr><td>Sector {d['sector']}, camera {d['camera']}, detector {d['ccd']}, {d['star_type']} stars</td><td>{d['searched']} of {d['patches']}</td><td>{d['stars_searched']}</td><td>{d['leads_found']}</td></tr>"
                    for d in sk["detectors"]) or '<tr><td colspan="4" class="none">No patches mapped yet.</td></tr>'
    openers = "".join(f"<li>{e(o['handle'])} <span>{o['patches']} patches, {o['stars']} stars</span></li>" for o in sk["openers"]) or "<li class=none>Nobody yet.</li>"
    active = "".join(f"<li>{e(h['handle'])} <span>{h['leads_vetted']} vetted</span></li>" for h in b["most_active"]) or "<li class=none>No hunters yet.</li>"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kepler board</title><style>
:root{{--bg:#fbfaf7;--fg:#1c1b19;--mut:#6b675f;--line:#dedad2;--acc:#8a5a00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#131210;--fg:#ece9e2;--mut:#9a958a;--line:#2e2c28;--acc:#e0b060}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.5 ui-sans-serif,system-ui,sans-serif;margin:0;padding:32px 16px}}
main{{max-width:760px;margin:0 auto}} h1{{font-size:28px;margin:0 0 4px}} h2{{font-size:18px;margin:32px 0 8px}}
p.sub{{color:var(--mut);margin:0 0 24px}} .tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}}
.tile{{border:1px solid var(--line);border-radius:8px;padding:12px}} .tile b{{display:block;font-size:26px;font-variant-numeric:tabular-nums}}
.tile span{{color:var(--mut);font-size:13px}} .wrap{{overflow-x:auto}} table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}} th{{color:var(--mut);font-weight:500;font-size:13px}}
.none{{color:var(--mut)}} ul{{list-style:none;padding:0;columns:2}} li span{{color:var(--mut);font-size:13px}} .note{{color:var(--mut);font-size:14px;margin-top:32px;border-top:1px solid var(--line);padding-top:16px}}
.surv b{{color:var(--acc)}}</style></head><body><main>
<h1>Kepler board</h1><p class="sub">Planet hunters and their AI assistants searching NASA TESS data and vetting what they find.</p>
<div class="tiles"><div class="tile surv"><b>{b['leads_surviving']}</b><span>surviving candidates</span></div>
<div class="tile"><b>{b['leads_cleared']}</b><span>leads cleared</span></div><div class="tile"><b>{b['leads_open']}</b><span>leads open</span></div>
<div class="tile"><b>{b['hunters']}</b><span>planet hunters</span></div><div class="tile"><b>{st['stars_searched']}</b><span>stars searched</span></div></div>
<h2>Surviving candidates</h2><div class="wrap"><table><tr><th>Star</th><th>Period</th><th>Depth</th><th>Vetted by</th></tr>{rows(b['surviving'], 'vetted_by')}</table></div>
<h2>Waiting for a second vet</h2><div class="wrap"><table><tr><th>Star</th><th>Period</th><th>Depth</th><th>First vet</th></tr>{rows(b['awaiting_second_vet'], 'first_vet_by')}</table></div>
<h2>Sky searched</h2><div class="wrap"><table><tr><th>Where</th><th>Patches searched</th><th>Stars</th><th>Leads found</th></tr>{cover}</table></div>
<h2>Who opened the sky</h2><ul>{openers}</ul>
<h2>Most active planet hunters</h2><ul>{active}</ul>
<p class="note">{e(b['note'])} A lead survives when two different planet hunters call it a candidate and the server's own tests agree.</p>
</main></body></html>"""
    return HTMLResponse(page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true", help="run over stdio for a local client")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--leads", default=os.environ.get("KEPLER_LEADS", ""), help="JSON-lines leads file to import at start")
    args = ap.parse_args()
    if args.leads:
        n = leads.load(args.leads)
        print(f"leads: imported {n} new, {leads.count()} in the queue", file=sys.stderr)
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
    # responses are event streams so progress notifications can travel with a running call
    mcp.run("streamable-http", host=args.host, port=args.port, stateless_http=True, json_response=False, transport_security=sec)


if __name__ == "__main__":
    main()
