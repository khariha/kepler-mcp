"""Offline tests: no network, no pixels. Run with `pytest`."""
import json, os, time
import numpy as np
import pytest


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    from tesshunt_mcp import store
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    store._hits.clear(); store._jobs.clear()
    return tmp_path


def V(**kw):
    v = dict(odd_depth_ppm=10000, even_depth_ppm=10100, depth_err_ppm=500, secondary_depth_ppm=100,
             secondary_any_phase_ppm=800, secondary_any_phase_at=0.4, n_transits=6)
    v.update(kw); return v


def test_candidate_bar_passes_clean_signal():
    from tesshunt.sweep_core import candidate_bar
    ok, fails = candidate_bar(15, 10000, 1.3, 3.9, 1.5, V())
    assert ok and fails == []


@pytest.mark.parametrize("kw,args,word", [
    (dict(secondary_any_phase_ppm=9000, secondary_any_phase_at=0.54), (15, 38000, 2.4, 11.08, 2.5), "phase 0.54"),   # eccentric eclipsing binary
    (dict(), (29, 8000, 6.0, 0.49, 1.0), "physical maximum"),                                                      # spotted rotator
    (dict(odd_depth_ppm=10000, even_depth_ppm=4000), (15, 10000, 1.3, 3.9, 1.5), "odd/even"),
    (dict(), (6.5, 10000, 1.3, 3.9, 1.5), "SNR"),
    (dict(n_transits=1), (15, 10000, 1.3, 3.9, 1.5), "transit(s)"),
    (dict(odd_depth_ppm=float("nan")), (15, 10000, 1.3, 3.9, 1.5), "odd/even"),
])
def test_candidate_bar_rejections(kw, args, word):
    from tesshunt.sweep_core import candidate_bar
    ok, fails = candidate_bar(*args, V(**kw))
    assert not ok and any(word in f for f in fails), fails


def test_clean_makes_json_safe():
    from tesshunt_mcp import store
    out = store.clean({"a": np.float64("nan"), "b": np.int64(3), "c": [np.float32(1.23456789), float("inf")], "d": np.bool_(True)})
    assert out == {"a": None, "b": 3, "c": [pytest.approx(1.234568, rel=1e-6), None], "d": True}
    assert store.clean(2458325.7244012345) == 2458325.7244                  # a BJD epoch keeps its decimals
    json.dumps(out)


def test_cache_roundtrip_and_key_stability():
    from tesshunt_mcp import store
    k1 = store.key("t", {"tic": 1, "p": 3.927167}); k2 = store.key("t", {"p": 3.927167, "tic": 1})
    assert k1 == k2 and k1 != store.key("t", {"tic": 2, "p": 3.927167})
    assert store.cache_get("t", k1) is None
    store.cache_put("t", k1, {"x": 1}, b"png")
    assert store.cache_get("t", k1) == ({"x": 1}, b"png")


def test_rate_limit_window(monkeypatch):
    from tesshunt_mcp import store
    monkeypatch.setitem(store.LIMITS, "heavy", 2)
    assert store.allow("s", "heavy", now=1000)[0] and store.allow("s", "heavy", now=1001)[0]
    ok, wait = store.allow("s", "heavy", now=1002)
    assert not ok and 3590 < wait <= 3600
    assert store.allow("other", "heavy", now=1002)[0]          # per session
    assert store.allow("s", "heavy", now=1000 + 3601)[0]       # window slides


def test_session_id_is_anonymous_and_stable():
    from tesshunt_mcp import store
    a = store.session_id({"x-forwarded-for": "203.0.113.9, 10.0.0.1", "user-agent": "claude"})
    assert a == store.session_id({"x-forwarded-for": "203.0.113.9", "user-agent": "claude"})
    assert a != store.session_id({"x-forwarded-for": "203.0.113.10", "user-agent": "claude"})
    assert "203" not in a and len(a) == 12


def test_trace_has_no_address(tmp_data):
    from tesshunt_mcp import store
    store.trace("abc", "vet_signal", {"tic": 1}, {"verdict": "on_target"}, 1.2, False)
    store.trace("abc", "vet_signal", {"tic": 1}, None, 0.1, False, error="boom")
    lines = [json.loads(l) for l in open(next((tmp_data / "traces").iterdir()))]
    assert lines[0]["result"] == {"verdict": "on_target"} and lines[1]["error"] == "boom"
    assert set(lines[0]) == {"ts", "session", "tool", "args", "seconds", "cached", "consent", "result_sha1", "result"}


def test_job_runs_once_and_reports_failure():
    from tesshunt_mcp import store
    calls = []
    def fn(progress):
        calls.append(1); progress("sector 1"); return {"status": "repeats"}, None
    assert store.job_start("followup", "k" * 20, fn) == "queued"
    for _ in range(50):
        state, hit = store.job_get("followup", "k" * 20)
        if hit: break
        time.sleep(0.05)
    assert hit[0] == {"status": "repeats"} and store.job_start("followup", "k" * 20, fn) == "done" and len(calls) == 1
    store.job_start("followup", "f" * 20, lambda p: 1 / 0)
    for _ in range(50):
        state, hit = store.job_get("followup", "f" * 20)
        if state["state"] == "failed": break
        time.sleep(0.05)
    assert state["state"] == "failed" and "ZeroDivisionError" in state["error"]
    assert store.job_get("followup", "0" * 20)[0]["state"] == "unknown"


def test_server_registers_tools_and_guide():
    import anyio
    from tesshunt_mcp import server
    async def go():
        return {t.name for t in await server.mcp.list_tools()}
    names = anyio.run(go)
    assert names == {"resolve_star", "catalog_check", "light_curve", "search_transits", "vet_signal", "neighbors", "fold_plot",
                     "followup_start", "job_status", "report_verdict", "read_guide",
                     "lesson", "join", "next_lead", "submit_vet", "board", "my_progress",
                     "sky_map", "open_patch", "patch_status", "welcome"}
    g = server._guide()
    import re as _re
    assert "eccentric" in g and not _re.search(r"TIC \d", g)          # the reference names no particular star
    assert "sweep" not in " ".join(names) and "mcmc" not in " ".join(names)


def test_verdict_links_to_evidence(tmp_data):
    from tesshunt_mcp import store
    store._seen.clear()
    store.trace("sid", "search_transits", {"tic": 7, "sector": 1}, {"tic": 7, "snr": 12.0}, 1.0, False)
    store.trace("sid", "vet_signal", {"tic": 7, "sector": 1}, {"tic": 7, "verdict": "on_target"}, 1.0, False)
    store.trace("sid", "vet_signal", {"tic": 8, "sector": 1}, {"tic": 8}, 1.0, False)
    seen = store.seen("sid", 7)
    assert [t for t, _ in seen] == ["search_transits", "vet_signal"] and all(len(h) == 16 for _, h in seen)
    assert store.seen("other", 7) == []


def _lc(depth, sec_depth=0.0, sec_phase=0.54, period=3.0, dur=0.1, n=6000, noise=0.002, red=0.0, seed=1):
    import lightkurve as lk
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 27, n); f = 1 + rng.normal(0, noise, n) + red * np.sin(2 * np.pi * t / 0.37)
    ph = (t % period) / period
    f[np.abs(((ph + 0.5) % 1) - 0.5) < dur / period / 2] -= depth
    f[np.abs(((ph - sec_phase + 0.5) % 1) - 0.5) < dur / period / 2] -= sec_depth
    return lk.LightCurve(time=t, flux=f)


def test_secondary_found_when_real_even_off_half_phase():
    from tesshunt import pipeline as P
    v = P.vet(_lc(0.03, sec_depth=0.008), 3.0, 0.0, 0.1)
    assert v["has_secondary"] and abs(v["secondary_any_phase_at"] - 0.54) < 0.02 and v["secondary_any_phase_sigma"] > 5


def test_no_secondary_called_on_noise_or_red_noise():
    from tesshunt import pipeline as P
    for seed in range(8):
        assert not P.vet(_lc(0.01, seed=seed), 3.0, 0.0, 0.1)["has_secondary"]
        assert not P.vet(_lc(0.01, red=0.0015, noise=0.001, seed=seed), 3.0, 0.0, 0.1)["has_secondary"]


def test_short_transit_allowed_exposure_smearing():
    from tesshunt.sweep_core import candidate_bar
    assert not candidate_bar(40, 1500, 0.75, 0.46, 0.5, V(depth_err_ppm=90), min_depth_ppm=0)[0]
    assert candidate_bar(40, 1500, 0.75, 0.46, 0.5, V(depth_err_ppm=90, has_secondary=False), min_depth_ppm=0, cadence_h=0.5)[0]


# ---------------- leads queue ----------------
def _leads(tmp_data, n=3):
    from tesshunt_mcp import leads
    p = tmp_data / "l.jsonl"
    p.write_text("\n".join(json.dumps({"tic": 100 + i, "sector": 7, "period_d": 2.5 + i, "t0": 1500.1, "duration_h": 1.5,
                                         "depth_ppm": 9000, "snr": 20 - i, "star": {"Tmag": 13.5}}) for i in range(n)))
    assert leads.load(str(p)) == n and leads.load(str(p)) == 0 and leads.count() == n
    return leads


def test_join_validates_and_keeps_names_unique(tmp_data):
    from tesshunt_mcp import leads
    a = leads.join("star_gazer", "pass-word")
    assert a["handle"] == "star_gazer" and len(a["hunter_id"]) == 12 and a["returning"] is False
    again = leads.join("Star_Gazer", "pass-word")                 # same handle (any case) and word: signed back in with the same id
    assert again["hunter_id"] == a["hunter_id"] and again["returning"] is True and again["handle"] == "star_gazer"
    with pytest.raises(leads.LeadError, match=r"different recovery word.*available: star_gazer2, star_gazer3, star_gazer7"):
        leads.join("star_gazer", "wrong-word")
    with pytest.raises(leads.LeadError, match="recovery_word must be"):
        leads.join("newname", "abc")
    assert leads.join("other", "pass-word")["hunter_id"] != a["hunter_id"]      # the id depends on the handle too
    for bad in ("ab", "has space", "x" * 21, "-lead", "<script>"):
        with pytest.raises(leads.LeadError):
            leads.join(bad, "pass-word")
    with pytest.raises(leads.LeadError, match="unknown hunter_id"):
        leads.claim_next("not-a-real-id")


def test_claims_spread_resume_and_expire(tmp_data):
    leads = _leads(tmp_data)
    a, b = leads.join("ann", "pass-word")["hunter_id"], leads.join("bob", "pass-word")["hunter_id"]
    la = leads.claim_next(a, now=1000)
    assert leads.claim_next(a, now=1100)["lead_id"] == la["lead_id"] and leads.claim_next(a, now=1100)["resumed"]
    lb = leads.claim_next(b, now=1000)
    assert lb["lead_id"] != la["lead_id"]                      # two hunters do not start on the same lead
    later = leads.claim_next(a, now=1000 + leads.CLAIM_SECONDS + 1)
    assert later["resumed"] is False                           # the old claim lapsed


def test_agreeing_non_candidate_clears_in_one_vet(tmp_data):
    leads = _leads(tmp_data, 1)
    a = leads.join("ann", "pass-word")["hunter_id"]
    lead = leads.claim_next(a)
    status, info = leads.record(a, lead["lead_id"], "eclipsing_binary", False, "secondary", "second dip", "eclipsing_binary", [])
    assert status == "cleared" and leads.claim_next(a) is None
    with pytest.raises(leads.LeadError, match="already"):
        leads.record(a, lead["lead_id"], "candidate", False, "other", "", "candidate_like", [])


def test_survival_needs_two_hunters_and_the_server(tmp_data):
    leads = _leads(tmp_data, 2)
    a, b, c = (leads.join(n, "pass-word")["hunter_id"] for n in ("ann", "bob", "cyd"))
    lead = leads.claim_next(a)["lead_id"]
    assert leads.record(a, lead, "candidate", False, "recovery", "repeats", "candidate_like", [])[0] == "open"
    assert leads.claim_next(b)["lead_id"] == lead              # the half-vetted lead is offered next
    assert leads.record(b, lead, "candidate", False, "recovery", "repeats", "candidate_like", [])[0] == "surviving"
    bd = leads.board()
    assert bd["leads_surviving"] == 1 and bd["surviving"][0]["vetted_by"] == ["ann", "bob"]
    # a lead the server does not believe never survives, however many hunters vote for it
    other = leads.claim_next(a)["lead_id"]
    for h in (a, b):
        assert leads.record(h, other, "candidate", False, "other", "looks good", "eclipsing_binary", [])[0] == "open"
    assert leads.record(c, other, "candidate", False, "other", "looks good", "eclipsing_binary", [])[0] == "cleared"
    assert leads.board()["leads_surviving"] == 1
    assert leads.stats(a) == {"handle": "ann", "leads_vetted": 2, "verdicts": {"candidate": 2}, "on_surviving_leads": 1}


def test_hold_votes_do_not_count(tmp_data):
    leads = _leads(tmp_data, 1)
    a, b = leads.join("ann", "pass-word")["hunter_id"], leads.join("bob", "pass-word")["hunter_id"]
    lead = leads.claim_next(a)["lead_id"]
    leads.record(a, lead, "candidate", True, "dilution", "grazing", "candidate_like", [])
    assert leads.record(b, lead, "candidate", False, "recovery", "ok", "candidate_like", [])[0] == "open"


def test_lead_filters_and_availability(tmp_data):
    from tesshunt_mcp import leads
    p = tmp_data / "f.jsonl"
    rows = [{"tic": 1, "sector": 7, "period_d": 2.0, "t0": 1.0, "duration_h": 1.0, "depth_ppm": 10000, "snr": 20, "star": {"Teff": 3300, "rad": 0.4}},   # red, 4.4 Re
            {"tic": 2, "sector": 7, "period_d": 3.0, "t0": 1.0, "duration_h": 2.0, "depth_ppm": 300, "snr": 15, "star": {"Teff": 5800, "rad": 1.0}},     # yellow, 1.9 Re
            {"tic": 3, "sector": 7, "period_d": 4.0, "t0": 1.0, "duration_h": 1.0, "depth_ppm": 900, "snr": 12, "star": {"Teff": 3200, "rad": 0.3}}]     # red, 0.98 Re
    p.write_text("\n".join(json.dumps(r) for r in rows)); leads.load(str(p))
    h = leads.join("ann", "pass-word")["hunter_id"]
    assert leads.available(h) == {"open_for_you": 3, "by_star_type": {"red": 2, "yellow": 1}, "by_size_if_planet": {"neptune": 1, "sub_neptune": 1, "rocky": 1}}
    first = leads.claim_next(h)
    assert first["tic"] == 1 and first["star_type"] == "red" and first["size_if_planet"] == "neptune"
    y = leads.claim_next(h, want_star="yellow")                 # the held red lead is released for the one asked for
    assert y["tic"] == 2 and y["resumed"] is False
    assert leads.claim_next(h, want_size="rocky")["tic"] == 3
    assert leads.claim_next(h, want_star="white") is None


# ---------------- sky map ----------------
def _sky(tmp_data, n_stars=7, size=3):
    from tesshunt_mcp import sky, leads
    sky.PATCH_SIZE = size
    stars = [{"tic": 1000 + i, "ra": 100 + 0.1 * i, "dec": -30 + 0.01 * i, "Tmag": 13.0, "Teff": 3300, "rad": 0.4} for i in range(n_stars)]
    assert sky.build_ccd(7, 2, 3, stars) == 3 and sky.build_ccd(7, 2, 3, stars) == 0
    return sky, leads


def _row(tic, **kw):
    r = dict(tic=tic, status="ok", period_d=3.0, t0=1500.0, duration_h=1.2, max_duration_h=1.5, depth_ppm=200, depth_err_ppm=300, snr=4.0, n_transits=8, cadence_min=30)
    r.update(kw); return r


def test_patches_are_built_claimed_and_released(tmp_data):
    sky, leads = _sky(tmp_data)
    s = sky.summary()
    assert s["totals"]["patches"] == 3 and s["totals"]["unopened"] == 3 and s["detectors"][0]["ccd"] == 3
    a, b = leads.join("ann", "pass-word")["hunter_id"], leads.join("bob", "pass-word")["hunter_id"]
    p = sky.claim(a, now=1000)
    assert p["id"] == "s7-c2-3-r01" and sky.claim(a, now=1001)["resumed"]
    with pytest.raises(sky.SkyError, match="already claimed by ann"):
        sky.claim(b, "s7-c2-3-r01", now=1002)
    assert sky.claim(b, now=1002)["id"] == "s7-c2-3-r02"
    assert len(sky.stars_for(a, "s7-c2-3-r01")["stars"]) == 3
    with pytest.raises(sky.SkyError, match="not held by this handle"):
        sky.stars_for(b, "s7-c2-3-r01")
    assert sky.claim(b, "s7-c2-3-r01", now=1000 + sky.CLAIM_SECONDS + 1)["opened_by"] == "bob"      # ann's hold lapsed


def test_lead_bar_keeps_transit_shapes_and_drops_rotators():
    from tesshunt_mcp import sky
    assert sky.lead_bar(_row(1, snr=12, depth_ppm=9000, depth_err_ppm=600))
    assert not sky.lead_bar(_row(1))                                                              # weak
    assert not sky.lead_bar(_row(1, snr=12, depth_ppm=9000, depth_err_ppm=600, duration_h=6.0))    # too long
    assert not sky.lead_bar(_row(1, snr=12, depth_ppm=9000, depth_err_ppm=600, period_d=0.3, duration_h=1.2, max_duration_h=2.0))   # duty cycle
    assert not sky.lead_bar(_row(1, snr=12, depth_ppm=9000, depth_err_ppm=600, n_transits=1))
    assert not sky.lead_bar(_row(1, status="error: boom")) and not sky.lead_bar({"tic": 1, "status": "ok", "snr": "nan"})


def test_upload_is_validated_and_flags_only_real_shapes(tmp_data):
    sky, leads = _sky(tmp_data)
    a = leads.join("ann", "pass-word")["hunter_id"]
    pid = sky.claim(a)["id"]
    tics = [s["tic"] for s in sky.stars_for(a, pid)["stars"]]
    with pytest.raises(sky.SkyError, match="has to finish"):
        sky.submit(a, pid, [_row(tics[0])])
    with pytest.raises(sky.SkyError, match="one row per star"):
        sky.submit(a, pid, [_row(t) for t in range(50)])
    rows = [_row(tics[0], snr=15, depth_ppm=9000, depth_err_ppm=500), _row(tics[1]), _row(tics[2]), _row(999999, snr=99, depth_ppm=50000, depth_err_ppm=100)]
    flagged, quiet = sky.submit(a, pid, rows)
    assert [f["tic"] for f in flagged] == [tics[0]] and quiet in tics[1:]                          # the star not in the patch is ignored
    assert sky.get(pid)["status"] == "verifying"
    out = sky.finish(pid, [{"tic": tics[0]}])
    assert out == {"patch_id": pid, "status": "done", "opened_by": "ann", "n_stars": 3, "n_leads": 1}
    s = sky.summary()
    assert s["totals"]["searched"] == 1 and s["totals"]["leads_found"] == 1 and s["openers"][0]["handle"] == "ann"


def test_rejected_patch_reopens(tmp_data):
    sky, leads = _sky(tmp_data)
    a = leads.join("ann", "pass-word")["hunter_id"]
    pid = sky.claim(a)["id"]
    tics = [s["tic"] for s in sky.stars_for(a, pid)["stars"]]
    sky.submit(a, pid, [_row(t) for t in tics])
    assert sky.finish(pid, [], rejected_reason="upload disagrees with the hub's own search")["status"] == "rejected"
    p = sky.get(pid)
    assert p["status"] == "unopened" and p["opened_by"] is None and "disagrees" in p["note"]


def test_claim_limit(tmp_data):
    sky, leads = _sky(tmp_data)
    a = leads.join("ann", "pass-word")["hunter_id"]
    sky.claim(a, "s7-c2-3-r01"); sky.claim(a, "s7-c2-3-r02")
    with pytest.raises(sky.SkyError, match="finish one first"):
        sky.claim(a, "s7-c2-3-r03")


def test_helper_worker_is_importable_and_real_leads_come_first(tmp_data):
    import pickle
    from kepler_sweep.worker import search_one
    assert pickle.loads(pickle.dumps(search_one)) is search_one          # worker processes must be able to load it by name
    from kepler_sweep.__main__ import looks_like_a_signal
    assert looks_like_a_signal(_row(1, snr=12, depth_ppm=9000, depth_err_ppm=600)) and not looks_like_a_signal(_row(1)) and not looks_like_a_signal({"status": "ok"})
    leads = _leads(tmp_data, 2)                                           # two file-loaded leads
    assert not leads.has_real()
    new = leads.add({"tic": 555, "sector": 7, "period_d": 1.5, "t0": 1500.0, "duration_h": 1.0, "depth_ppm": 8000, "snr": 9.0, "star": {"Teff": 3300, "rad": 0.4}},
                    found_by="cara", patch_id="s7-c1-1-02")
    assert new and leads.has_real()
    assert leads.add({"tic": 555, "sector": 7, "period_d": 1.5, "t0": 1500.0, "duration_h": 1.0, "depth_ppm": 8000, "snr": 9.0}) is None   # no duplicates
    h = leads.join("dan", "pass-word")["hunter_id"]
    first = leads.claim_next(h)
    assert first["tic"] == 555 and first["found_by"] == "cara" and first["practice"] is False     # a searched-patch lead outranks file leads


def test_patches_by_star_type(tmp_data):
    sky, leads = _sky(tmp_data)                                   # three red patches
    ys = [{"tic": 2000 + i, "ra": 100 + 0.1 * i, "dec": -30.0, "Tmag": 14.0, "Teff": 5700, "rad": 1.0} for i in range(4)]
    assert sky.build_ccd(7, 2, 3, ys, "yellow") == 2
    s = sky.summary()
    assert s["unopened_by_star_type"] == {"red": 3, "yellow": 2} and set(s["star_types"]) == {"red", "orange", "yellow"}
    a = leads.join("ann", "pass-word")["hunter_id"]
    y = sky.claim(a, pop="yellow")
    assert y["id"] == "s7-c2-3-y01" and y["pop"] == "yellow"
    assert sky.claim(a, pop="red")["id"] == "s7-c2-3-r01"         # a held yellow patch does not block asking for red
    assert sky.claim(a, pop="yellow")["resumed"]
    assert sky.claim(leads.join("bob", "pass-word")["hunter_id"], pop="orange") is None


# ---------------- voice and result contract ----------------
def test_next_actions_entries_are_callable_shapes():
    import anyio
    from tesshunt_mcp import core, server
    a = core.act("vet_signal", "false-positive tests", tic=1, sector=7, period_d=np.float64(2.5), t0=1500.123456789, duration_h=1.0)
    assert a == {"tool": "vet_signal", "args": {"tic": 1, "sector": 7, "period_d": 2.5, "t0": 1500.12345679, "duration_h": 1.0}, "why": "false-positive tests"}
    json.dumps(a)
    async def go():
        return {t.name: t for t in await server.mcp.list_tools()}
    tools = anyio.run(go)
    # every tool name the server ever suggests must exist, and the arguments it suggests must be parameters of that tool
    import inspect, re
    src = inspect.getsource(server) + inspect.getsource(core)
    for name, kwargs in re.findall(r'act\("(\w+)",\s*"[^"]*"((?:,\s*\w+=[^,)]+)*)', src):
        assert name in tools, name
        params = set(tools[name].input_schema["properties"])
        for k in re.findall(r"(\w+)=", kwargs):
            assert k in params, (name, k)


def test_register_is_instrument_not_tour_guide():
    import anyio, inspect, re
    from tesshunt_mcp import core, leads, server, sky
    async def go():
        return await server.mcp.list_tools(), await server.mcp.list_prompts()
    tools, prompts = anyio.run(go)
    names = {t.name for t in tools}
    assert "start" not in {p.name for p in prompts} and "lesson" in names
    assert server.WELCOME.startswith("KEPLER MCP BY GENERATIVE EXPERIENCES") and "HOW TO USE" in server.WELCOME and "!" not in server.WELCOME
    texts = {"instructions": server.INSTRUCTIONS}
    for t in tools:
        if t.name not in ("lesson", "welcome"):                  # the lesson keeps its teaching voice; the welcome notice is its own test
            texts["tool " + t.name] = t.description or ""
    for k, v in {**core.SUMMARY_VET, **core.SUMMARY_FOLLOW}.items():
        texts["summary " + k] = v
    for k, v in sky.POPULATIONS.items():
        texts["population " + k] = v["finds"]
    texts["hunt prompt"] = server.hunt(); texts["check_star prompt"] = server.check_star("X")
    # every literal that feeds a summary or an error
    for mod in (core, leads, sky, server):
        src = inspect.getsource(mod)
        src = src.split("LESSON = {")[0] + src.split("LESSON = {")[1].split("\n}\n", 1)[1] if "LESSON = {" in src else src
        for i, m in enumerate(re.findall(r'(?:summary"\]?\s*[+=:]+\s*\(?f?|Error\(f?)"([^"]+)"', src)):
            texts[f"{mod.__name__} literal {i}"] = m
    banned = re.compile(r"(?i)\b(you|your|yours|let's|congratulations|great|awesome)\b|!")
    bad = {k: banned.search(v).group(0) for k, v in texts.items() if banned.search(v)}
    assert not bad, bad


def test_live_progress_display(tmp_data):
    sky, leads = _sky(tmp_data)
    sky._progress.clear()
    a, b = leads.join("ann", "pass-word")["hunter_id"], leads.join("bob", "pass-word")["hunter_id"]
    pid = sky.claim(a)["id"]
    assert sky.progress(pid) is None
    with pytest.raises(sky.SkyError):
        sky.set_progress(b, pid, 1, 3, 0)                              # only the holder may report
    sky.set_progress(a, pid, 0, 3, 0, now=1000)
    sky.set_progress(a, pid, 2, 3, 1, "signal TIC 1001: depth 1.01%, P = 0.455 d", now=1060)
    g = sky.progress(pid, now=1060)
    assert g["stars_done"] == 2 and g["percent"] == 66 and g["signals_so_far"] == 1 and g["minutes_left"] == 0.5
    d = g["display"]
    assert "FIELD " + pid in d and "2/3 stars" in d and "\u2588" in d and "TIC 1001" in d and "DOWNLINK + SEARCH" in d and "T+01:00" in d
    sky._progress.clear(); sky.set_progress(a, pid, 0, 3, 0, now=1000)
    assert "ACQUISITION" in sky.progress(pid, now=1001)["display"]                # something to show before the first star finishes
    sky.set_progress(a, pid, 2, 3, 0, stars=["TIC 1000  T 13.0  rms 0.21%  nominal"], now=1060)
    assert "nominal" in sky.progress(pid, now=1060)["display"]
    assert sky.progress(pid, now=1060 + 601) is None                   # stale readings are not shown
