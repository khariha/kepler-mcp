"""Every workflow through the live server, following only next_actions, printing each readback as a client would."""
import anyio, json
from mcp import Client
async def go():
    async with Client("" + (__import__("sys").argv[1] if len(__import__("sys").argv) > 1 else "http://127.0.0.1:8000/mcp") + "", read_timeout_seconds=200) as c:
        async def call(name, args):
            r = await c.call_tool(name, args); t = next((b.text for b in r.content if b.type == "text"), "")
            assert not r.is_error, (name, args, t[:300])
            d = json.loads(t)
            assert isinstance(d, dict) and d.get("display"), (name, "no display")
            assert all(len(x) <= 90 for x in d["display"].split("\n")), name
            print(f"--- {name}\n{d['display']}\n")
            return d
        nxt = lambda d, tool: [a for a in d["next_actions"] if a["tool"] == tool][0]["args"]
        await call("find_planets", {})
        h = await call("join", {"handle": "chain_" + __import__("secrets").token_hex(3), "recovery_word": "pass-word"})
        lead = await call("next_lead", nxt(h, "next_lead"))
        await call("catalog_check", nxt(lead, "catalog_check"))
        v = await call("vet_signal", nxt(lead, "vet_signal"))
        await call("neighbors", {"tic": lead["tic"], "sector": lead["sector"], "depth_ppm": lead["depth_ppm"]})
        j = await c.call_tool("followup_start", nxt(v, "followup_start")); job = json.loads(j.content[0].text)["job_id"]     # vet's suggestion, measured depth
        n = 0
        while True:
            f = await call("job_status", {"job_id": job}); n += 1
            if f["state"] in ("done", "failed"): break
        print(f"(follow-up took {n} status calls)\n")
        await call("submit_vet", lead["submit"]["args"] | {"verdict": "known_object", "reason": "catalogued planet", "deciding_test": "catalog"})
        s = await call("resolve_star", {"query": "TIC 61994665"})
        await call("light_curve", {"tic": s["ID"], "sector": 7})
        await call("search_transits", nxt(s, "search_transits"))
        await call("sky_map", {})
        o = await call("open_patch", {"hunter_id": h["hunter_id"]})
        print("keep_polling on an unmapped sky:", o.get("keep_polling"))
anyio.run(go)
