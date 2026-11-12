"""Smoke-test a running tess-hunt MCP server and warm its cache.

    python scripts/warm.py http://127.0.0.1:8000/mcp                 # default: Pi Mensae, sector 1
    python scripts/warm.py https://your.host/mcp "TOI-700" "TIC 410153553"

For each star: resolve, catalog check, search the earliest sector, vet the best peak, list neighbours, run the multi-sector follow-up.
Exit code 0 when every call succeeded. It prints what the server found; it does not assert what a star should be.
"""
import json, sys, time
import anyio
from mcp import Client


async def main(url, stars):
    errors = 0
    async with Client(url, read_timeout_seconds=200) as c:
        async def call(name, **args):
            nonlocal errors
            t = time.time()
            r = await c.call_tool(name, args)
            text = next((b.text for b in r.content if b.type == "text"), "")
            errors += bool(r.is_error)
            print(f"  {name:16s} {time.time()-t:6.1f}s  {'ERROR ' + text[:200] if r.is_error else 'ok'}")
            return None if r.is_error else json.loads(text)
        for q in stars:
            print(q)
            s = await call("resolve_star", query=q)
            if not s:
                continue
            tic, sec = s["ID"], s["sectors"][0]
            c0 = await call("catalog_check", tic=tic)
            await call("light_curve", tic=tic, sector=sec)
            r = await call("search_transits", tic=tic, sector=sec)
            if not r:
                continue
            b = r["best"]
            v = await call("vet_signal", tic=tic, sector=sec, period_d=b["period_d"], t0=b["t0"], duration_h=b["duration_h"])
            await call("neighbors", tic=tic, sector=sec, depth_ppm=b["depth_ppm"])
            j = await call("followup_start", tic=tic, period_d=b["period_d"], t0=b["t0"], duration_h=b["duration_h"], depth_ppm=b["depth_ppm"])
            f = None
            for _ in range(80):
                f = await call("job_status", job_id=j["job_id"]) if j else None
                if not f or f["state"] in ("done", "failed"):
                    break
                await anyio.sleep(15)
            print(f"  -> TIC {tic} sector {sec}: P={b['period_d']:.4f} d depth={b['depth_ppm']:.0f} ppm SNR={b['snr']:.1f} bar={'pass' if r['passes_candidate_bar'] else 'fail'}; "
                  f"known host={c0 and c0['known_planet_host']}; vet={v and v['verdict']}; follow-up={f and f.get('status')} "
                  f"({f and len(f.get('sectors_agreeing', []))} of {f and f.get('usable_sectors')} sectors agree, {f and len(f.get('sectors_contradicting', []))} contradict)")
    return errors


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/mcp"
    sys.exit(1 if anyio.run(main, url, sys.argv[2:] or ["Pi Mensae"]) else 0)
