"""Search one patch of sky on this computer and upload the results to Kepler.

    kepler-sweep s7-c2-2-01 --hunter <hunter_id> [--hub https://...] [--workers 8]
    kepler-sweep --mcp            # run as a local MCP server with one tool, sweep_patch

It fetches the patch's star list from the hub, reads each star's pixels straight from NASA's public TESS files, runs the transit
search, and uploads one row of numbers per star. Nothing else leaves this computer. An interrupted run picks up where it stopped.
"""
import argparse, json, os, sys, time, urllib.error, urllib.request, warnings
from concurrent.futures import ProcessPoolExecutor, as_completed

DEFAULT_HUB = os.environ.get("KEPLER_HUB", "http://127.0.0.1:8000")
STATE = os.path.join(os.path.expanduser("~"), ".kepler-sweep")
from kepler_sweep.worker import search_one


def _http(url, payload=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "kepler-sweep"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except Exception:
            msg = str(e)
        raise SystemExit(f"error (hub): {msg}")
    except urllib.error.URLError as e:
        raise SystemExit(f"error: hub unreachable at {url.split('/patch')[0]} ({e.reason})")


def telemetry(r):
    """One short line per finished star for the live readout."""
    tm = f"T {r['tmag']:.1f}" if r.get("tmag") is not None else "T  ?  "
    if r.get("status") != "ok":
        return f"TIC {r['tic']:<11} {tm}  no usable light curve"
    rms = f"rms {r['noise_ppm'] / 1e4:.2f}%" if r.get("noise_ppm") else "rms   ?  "
    return f"TIC {r['tic']:<11} {tm}  {rms}  " + ("TRANSIT-LIKE SIGNAL" if looks_like_a_signal(r) else "nominal")


def _beat(hub, patch, hunter, done, total, signals, latest=None, stars=None):
    """Tell the hub how far the search has got, so the assistant can show it. Never fatal."""
    try:
        _http(f"{hub}/patch/{patch}/progress", {"hunter": hunter, "done": done, "total": total, "signals": signals, "latest": latest, "stars": stars or []}, timeout=8)
    except BaseException:
        pass


def out(msg=""):
    """Print at once. Piped output is block-buffered by default, which hid all progress when an assistant ran the helper."""
    print(msg, flush=True)


def bar(done, total, width=24):
    full = int(width * done / max(total, 1))
    return "\u2595" + "\u2588" * full + "\u2591" * (width - full) + "\u258f"


def looks_like_a_signal(r):
    """Rough local count for the progress line; the hub applies the real rule."""
    try:
        return r["status"] == "ok" and r["snr"] >= 8 and r["n_transits"] >= 2 and r["depth_ppm"] / r["depth_err_ppm"] >= 5 and r["duration_h"] <= 1.5 * r["max_duration_h"] + 0.5
    except Exception:
        return False


def sweep(patch, hunter, hub=DEFAULT_HUB, workers=None, say=out):
    """Search a patch and upload it. Returns the hub's final word on the patch as a dict."""
    hub = hub.rstrip("/")
    job = _http(f"{hub}/patch/{patch}?hunter={hunter}")
    stars, sector = job["stars"], job["sector"]
    os.makedirs(STATE, exist_ok=True)
    path = os.path.join(STATE, f"{patch}.jsonl")
    done = {}
    if os.path.exists(path):
        for line in open(path):
            try:
                r = json.loads(line); done[r["tic"]] = r
            except Exception:
                pass
    todo = [s for s in stars if s["tic"] not in done]
    workers = workers or max(2, min(8, (os.cpu_count() or 4) - 1))
    est = max(1, round(len(todo) / (2.7 * workers)))
    say(f"\U0001F6F0  KEPLER  field {patch}  |  TESS Sector {sector}  |  {len(stars)} target stars")
    say(f"   link 1/3  Kepler hub ............ target list received ({len(stars)} stars)" + (f", resuming at {len(done)}" if done else ""))
    say(f"   link 2/3  TESS archive (MAST) ... downlinking pixels and searching each star, {workers} in parallel, about {est} min")
    say("             What happens: for each star, read its pixels from NASA's public TESS full-frame images, build a light curve,")
    say("             and search it for a repeating transit-like dip. Only the resulting numbers are sent back.")
    t0 = time.time(); last = 0; first_error = None
    _beat(hub, patch, hunter, len(done), len(stars), sum(looks_like_a_signal(x) for x in done.values()))
    if todo:
        with ProcessPoolExecutor(workers) as ex, open(path, "a") as f:
            failures = 0; fresh = []
            for fut in as_completed([ex.submit(search_one, (s, sector)) for s in todo]):
                try:
                    r = fut.result()
                except Exception as e:
                    failures += 1; first_error = first_error or f"{type(e).__name__}: {e}"
                    if failures >= 5 and not done:
                        raise SystemExit(f"error: search cannot run here: {first_error}")
                    continue
                done[r["tic"]] = r
                f.write(json.dumps(r) + "\n"); f.flush()
                fresh.append(telemetry(r))
                if len(fresh) >= 3 and not looks_like_a_signal(r):
                    _beat(hub, patch, hunter, len(done), len(stars), sum(looks_like_a_signal(x) for x in done.values()), stars=fresh)
                    fresh = []
                if looks_like_a_signal(r):
                    line = f"TIC {r['tic']}: depth {r['depth_ppm']/1e4:.2f}%, P = {r['period_d']:.3f} d, {int(r['n_transits'])} transits, S/N {r['snr']:.1f}"
                    say(f"   \u25C9 signal  {line}")
                    _beat(hub, patch, hunter, len(done), len(stars), sum(looks_like_a_signal(x) for x in done.values()), "signal " + line, stars=fresh)
                    fresh = []
                if len(done) - last >= max(5, len(stars) // 15) or len(done) == len(stars):
                    last = len(done)
                    n_sig = sum(looks_like_a_signal(x) for x in done.values())
                    rate = (len(done) - (len(stars) - len(todo))) / max(time.time() - t0, 1e-9) * 60
                    left = (len(stars) - len(done)) / max(rate, 1e-9)
                    _beat(hub, patch, hunter, len(done), len(stars), n_sig)
                    say(f"   downlink {bar(len(done), len(stars))} {100 * len(done) // len(stars):3d}%  {len(done)}/{len(stars)} stars  {n_sig} signal(s)  {left:.1f} min left")
    if len(done) < 0.9 * len(stars):
        raise SystemExit(f"error: {len(done)} of {len(stars)} stars searched ({first_error if todo else 'no error recorded'}); nothing uploaded; rerun to resume")
    ok = sum(1 for r in done.values() if r["status"] == "ok")
    say(f"   search complete: {ok}/{len(stars)} usable light curves, {sum(looks_like_a_signal(x) for x in done.values())} transit-like signal(s)")
    res = _http(f"{hub}/patch/{patch}/results", {"hunter": hunter, "rows": list(done.values())})
    say(f"   link 3/3  Kepler hub ............ uplinked {len(done)} rows; hub is re-running {res.get('signals_flagged', 0)} flagged star(s) to verify")
    final = {"status": "verifying"}
    for _ in range(90):
        final = _http(f"{hub}/patch/{patch}/status")
        if final.get("status") != "verifying":
            break
        time.sleep(4)
    if final.get("status") == "done":
        n = final.get("n_leads", 0)
        say(f"\U0001F6F0  field {patch}: done. {n} transit-like signal(s) verified and queued for vetting." if n
            else f"\U0001F6F0  field {patch}: done. No signal passed verification; this field is now recorded as searched.")
        try:
            os.remove(path)
        except OSError:
            pass
    elif final.get("status") == "verifying":
        say(f"patch {patch}: hub still verifying; check patch_status")
    else:
        say(f"patch {patch}: upload not accepted: {final.get('note') or final.get('status')}")
    return final


def serve_mcp():
    """Local MCP server over stdio with one tool, for people who would rather not paste commands."""
    import anyio
    from mcp.server.mcpserver import Context, MCPServer
    m = MCPServer("kepler-sweep", title="Kepler sweep helper (runs on this computer)",
                  instructions="One tool: sweep_patch searches a patch of sky on this computer for the Kepler hub. Use it after Kepler's open_patch gives you a patch id.")

    @m.tool(title="Search a patch of sky on this computer")
    async def sweep_patch(patch_id: str, hunter_id: str, ctx: Context, hub: str = DEFAULT_HUB) -> dict:
        """Search one Kepler patch (about 150 stars, about seven minutes) on this computer and upload the results. Takes the patch_id and
        hunter_id from Kepler's open_patch. Reads NASA's public TESS data directly; only the search results are sent to the hub."""
        lines = []

        def say(msg):
            lines.append(msg)
            try:
                anyio.from_thread.run(ctx.report_progress, float(len(lines)), None, msg)
            except Exception:
                pass
        final = await anyio.to_thread.run_sync(lambda: sweep(patch_id, hunter_id, hub, None, say))
        return {"patch": final, "log": lines[-6:]}
    m.run("stdio")


def main():
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(prog="kepler-sweep", description="Search one patch of sky for Kepler on this computer.")
    ap.add_argument("patch", nargs="?", help="patch id from Kepler's open_patch, for example s7-c2-2-01")
    ap.add_argument("--hunter", help="your planet-hunter id (hunter_id) from Kepler's join")
    ap.add_argument("--hub", default=DEFAULT_HUB, help="address of the Kepler server")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--mcp", action="store_true", help="run as a local MCP server instead")
    a = ap.parse_args()
    if a.mcp:
        return serve_mcp()
    if not a.patch or not a.hunter:
        ap.error("give a patch id and --hunter (both come from Kepler's open_patch)")
    try:
        import lightkurve, astrocut  # noqa: F401
    except Exception:
        sys.exit("error: science libraries missing; install with: pip install kepler-mcp")
    final = sweep(a.patch, a.hunter, a.hub, a.workers)
    sys.exit(0 if final.get("status") in ("done", "verifying") else 1)


if __name__ == "__main__":
    main()
