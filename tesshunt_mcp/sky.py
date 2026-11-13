"""The sky map: which patches of which sectors have been searched, by whom, and what came back.

A patch is a compact group of stars from one TESS detector, sized to take a laptop about seven minutes. The hub decides what is in
each patch; a hunter's local helper searches it and uploads one row per star. Uploaded rows are untrusted: the hub re-runs every
would-be lead itself, plus one random quiet star, before anything enters the leads queue. No science lives here; the selection and
the re-check are passed in, so this module can be tested offline.
"""
import json, math, os, random, sqlite3, threading, time

from tesshunt_mcp import leads, store

PATCH_SIZE = int(os.environ.get("KEPLER_PATCH_SIZE", 150))     # measured: a laptop sweeps ~22 stars a minute
CLAIM_SECONDS = 2 * 3600
MAX_CLAIMS = 2
OPEN_SECTORS = [int(x) for x in os.environ.get("KEPLER_OPEN_SECTORS", "7 8").split()]
PAUSED = os.environ.get("KEPLER_PATCHES_PAUSED", "0") == "1"
_lock = threading.RLock()

# The kinds of star a patch can hold. Limits are passed to the target selection; `finds` is said to the planet hunter.
POPULATIONS = {
    "red": {"teff": (2300.0, 3900.0), "rad_max": 0.6, "tmag": (12.0, 15.0),
            "finds": "Red dwarfs, Tmag 12-15. Detectable: dips of about 0.5% and deeper, Neptune-sized planets and larger. Least-searched population."},
    "orange": {"teff": (3900.0, 5200.0), "rad_max": 0.85, "tmag": (12.5, 15.0),
               "finds": "Orange dwarfs, Tmag 12.5-15. Detectable: dips of about 0.7% and deeper, Saturn-sized planets and larger."},
    "yellow": {"teff": (5200.0, 6000.0), "rad_max": 1.3, "tmag": (13.5, 15.0),
               "finds": "Sun-like stars, Tmag 13.5-15 (brighter ones are covered by the mission's own searches). Detectable: dips near 1%, giant planets only."},
}

SCHEMA = """
create table if not exists patches (id text primary key, sector integer, camera integer, ccd integer, pop text default 'red', n integer, status text default 'unopened',
    opened_by text, claimed_until real, done_at real, n_stars integer, n_flagged integer default 0, n_leads integer default 0, note text);
create table if not exists patch_stars (patch_id text, tic integer, star text, primary key (patch_id, tic));
create table if not exists patch_rows (patch_id text, tic integer, row text, primary key (patch_id, tic));
create table if not exists ccds (sector integer, camera integer, ccd integer, pop text, status text, n_stars integer, built real, primary key (sector, camera, ccd, pop));
"""
ROW_FIELDS = ("status", "period_d", "t0", "duration_h", "max_duration_h", "depth_ppm", "depth_err_ppm", "snr", "n_transits", "cadence_min")


class SkyError(Exception):
    """A message the hunter's assistant can act on."""


def _db():
    os.makedirs(store.DATA, exist_ok=True)
    c = sqlite3.connect(os.path.join(store.DATA, "sky2.db"), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def patch_id(sector, camera, ccd, n, pop="red"):
    return f"s{sector}-c{camera}-{ccd}-{pop[0]}{n:02d}"


# ---------------- building patches ----------------
def build_ccd(sector, camera, ccd, stars, pop="red"):
    """Cut one detector's stars into patches. stars: list of dicts with tic, ra, dec and whatever else the helper needs.
    Stars are ordered along a serpentine path across the detector so each patch is a compact piece of sky."""
    stars = sorted(stars, key=lambda s: s["tic"])
    if stars:
        ra0 = min(s["ra"] for s in stars); dec0 = min(s["dec"] for s in stars)
        band = lambda s: int((s["dec"] - dec0) / 1.0)
        stars.sort(key=lambda s: (band(s), (s["ra"] - ra0) if band(s) % 2 == 0 else -(s["ra"] - ra0)))
    n_patches = max(1, math.ceil(len(stars) / PATCH_SIZE)) if stars else 0
    with _lock, _db() as c:
        if c.execute("select 1 from ccds where sector=? and camera=? and ccd=? and pop=? and status='ready'", (sector, camera, ccd, pop)).fetchone():
            return 0
        for i in range(n_patches):
            chunk = stars[i * PATCH_SIZE:(i + 1) * PATCH_SIZE]
            pid = patch_id(sector, camera, ccd, i + 1, pop)
            c.execute("insert or ignore into patches (id, sector, camera, ccd, pop, n, n_stars) values (?,?,?,?,?,?,?)", (pid, sector, camera, ccd, pop, i + 1, len(chunk)))
            c.executemany("insert or ignore into patch_stars values (?,?,?)", [(pid, int(s["tic"]), json.dumps(s)) for s in chunk])
        c.execute("insert or replace into ccds values (?,?,?,?,?,?,?)", (sector, camera, ccd, pop, "ready", len(stars), time.time()))
    return n_patches


def ccd_status(sector, camera, ccd, pop="red"):
    with _lock, _db() as c:
        r = c.execute("select status from ccds where sector=? and camera=? and ccd=? and pop=?", (sector, camera, ccd, pop)).fetchone()
        return r["status"] if r else None


def mark_ccd(sector, camera, ccd, status, pop="red"):
    with _lock, _db() as c:
        c.execute("insert or replace into ccds values (?,?,?,?,?,?,?)", (sector, camera, ccd, pop, status, 0, time.time()))


# ---------------- claiming ----------------
def _release_expired(c, now):
    c.execute("update patches set status='unopened', opened_by=null, claimed_until=null where status='claimed' and claimed_until < ?", (now,))


def claim(hunter_id, pid=None, now=None, pop=None):
    """Hold a patch for this hunter. With no id, picks an unopened one of the wanted star type (resuming a patch they already hold first)."""
    now = now or time.time()
    if PAUSED:
        raise SkyError("patch claims are paused; next_lead may still have leads")
    with _lock:
        with leads._db() as lc:
            me = leads._handle(lc, hunter_id)
        with _db() as c:
            _release_expired(c, now)
            held = c.execute("select * from patches where status='claimed' and opened_by=?", (me,)).fetchall()
            if pid is None:
                mine = [h for h in held if not pop or h["pop"] == pop]
                if mine:
                    return dict(mine[0]) | {"resumed": True}
                row = c.execute("select * from patches where status='unopened'" + (" and pop=?" if pop else "") + " order by sector, camera, ccd, n limit 1", (pop,) if pop else ()).fetchone()
                if not row:
                    return None
            else:
                row = c.execute("select * from patches where id=?", (pid,)).fetchone()
                if not row:
                    raise SkyError(f"there is no patch {pid}; call sky() to see what exists")
                if row["status"] == "claimed" and row["opened_by"] == me:
                    return dict(row) | {"resumed": True}
                if row["status"] != "unopened":
                    raise SkyError(f"patch {pid} is already {row['status']}" + (f" by {row['opened_by']}" if row["opened_by"] else "") + "; pick another")
            if len(held) >= MAX_CLAIMS:
                raise SkyError(f"claim limit reached: {len(held)} patches held ({', '.join(h['id'] for h in held)}); finish one first")
            c.execute("update patches set status='claimed', opened_by=?, claimed_until=? where id=?", (me, now + CLAIM_SECONDS, row["id"]))
            return dict(c.execute("select * from patches where id=?", (row["id"],)).fetchone()) | {"resumed": False}


def stars_for(hunter_id, pid):
    """The star list for a patch, for the hunter who holds it."""
    with _lock:
        with leads._db() as lc:
            me = leads._handle(lc, hunter_id)
        with _db() as c:
            p = c.execute("select * from patches where id=?", (pid,)).fetchone()
            if not p:
                raise SkyError(f"there is no patch {pid}")
            if p["status"] != "claimed" or p["opened_by"] != me:
                raise SkyError(f"patch {pid} is not held by this handle; call open_patch first")
            return {"patch_id": pid, "sector": p["sector"], "stars": [json.loads(r["star"]) for r in c.execute("select star from patch_stars where patch_id=? order by tic", (pid,))]}


# ---------------- uploads ----------------
def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def lead_bar(r, min_snr=8.0, min_sigma=5.0):
    """Would this search result make a lead? Looser than the candidate bar so hunters meet impostors too; rotators are dropped."""
    if r.get("status") != "ok":
        return False
    g = lambda k: _num(r.get(k))
    snr, n, depth, err, dur, mx, per = g("snr"), g("n_transits"), g("depth_ppm"), g("depth_err_ppm"), g("duration_h"), g("max_duration_h"), g("period_d")
    cad_h = (g("cadence_min") or 30.0) / 60
    if None in (snr, n, depth, err, dur, mx, per) or err <= 0 or per <= 0:
        return False
    return bool(snr >= min_snr and n >= 2 and depth > 0 and depth / err >= min_sigma
                and dur <= 1.5 * mx + cad_h + 1e-6 and dur / 24 / per < 0.15)


def submit(hunter_id, pid, rows, now=None):
    """Accept one row per star from the helper. Returns (flagged rows, one random quiet tic or None) for the hub to re-check."""
    now = now or time.time()
    with _lock:
        with leads._db() as lc:
            me = leads._handle(lc, hunter_id)
        with _db() as c:
            p = c.execute("select * from patches where id=?", (pid,)).fetchone()
            if not p:
                raise SkyError(f"there is no patch {pid}")
            if p["status"] != "claimed" or p["opened_by"] != me:
                raise SkyError(f"patch {pid} is not held by this handle (status: {p['status']})")
            want = {r["tic"] for r in c.execute("select tic from patch_stars where patch_id=?", (pid,))}
            if not isinstance(rows, list) or len(rows) > len(want) + 5:
                raise SkyError("results must be a list with one row per star in the patch")
            clean = {}
            for r in rows:
                tic = int(_num(r.get("tic")) or -1)
                if tic in want:
                    clean[tic] = {"tic": tic} | {k: (r.get(k) if k == "status" else _num(r.get(k))) for k in ROW_FIELDS}
                    clean[tic]["status"] = str(clean[tic]["status"])[:60]
            if len(clean) < 0.9 * len(want):
                raise SkyError(f"results cover {len(clean)} of {len(want)} stars; the search has to finish before uploading")
            c.execute("delete from patch_rows where patch_id=?", (pid,))
            c.executemany("insert into patch_rows values (?,?,?)", [(pid, t, json.dumps(r)) for t, r in clean.items()])
            flagged = [r for r in clean.values() if lead_bar(r)]
            quiet = [t for t, r in clean.items() if r["status"] == "ok" and not lead_bar(r)]
            c.execute("update patches set status='verifying', n_flagged=?, claimed_until=null where id=?", (len(flagged), pid))
            return flagged, (random.choice(quiet) if quiet else None)


def finish(pid, verified, rejected_reason=None, now=None):
    """Close a patch after the hub's re-check. verified: lead dicts ready for the queue. A rejected patch goes back to unopened."""
    now = now or time.time()
    with _lock, _db() as c:
        p = c.execute("select * from patches where id=?", (pid,)).fetchone()
        if rejected_reason:
            c.execute("delete from patch_rows where patch_id=?", (pid,))
            c.execute("update patches set status='unopened', opened_by=null, claimed_until=null, n_flagged=0, note=? where id=?", (rejected_reason[:200], pid))
            return {"patch_id": pid, "status": "rejected", "reason": rejected_reason}
        c.execute("update patches set status='done', done_at=?, n_leads=?, note=null where id=?", (now, len(verified), pid))
        return {"patch_id": pid, "status": "done", "opened_by": p["opened_by"], "n_stars": p["n_stars"], "n_leads": len(verified)}


_progress = {}
RULE = "\u2500" * 66


def set_progress(hunter_id, pid, done, total, signals, latest=None, now=None, stars=None):
    """Heartbeat from the helper while it searches. Only the handle holding the patch may report.
    latest: a line describing a new signal. stars: short telemetry lines for stars just completed."""
    with _lock:
        with leads._db() as lc:
            me = leads._handle(lc, hunter_id)
        with _db() as c:
            p = c.execute("select status, opened_by, n_stars, sector, pop from patches where id=?", (pid,)).fetchone()
        if not p or p["status"] != "claimed" or p["opened_by"] != me:
            raise SkyError(f"patch {pid} is not held by this handle")
        prev = _progress.get(pid) or {"started": now or time.time(), "signals_seen": [], "stars_seen": []}
        total = int(p["n_stars"]); done = max(0, min(int(done), total))
        seen = prev["signals_seen"]
        if latest and latest not in seen:
            seen = (seen + [str(latest)[:120]])[-5:]
        recent = (prev["stars_seen"] + [str(x)[:90] for x in (stars or [])][:6])[-4:]
        _progress[pid] = {"started": prev["started"], "updated": now or time.time(), "done": done, "total": total, "sector": p["sector"], "pop": p["pop"],
                          "signals": max(0, int(signals)), "signals_seen": seen, "stars_seen": recent}


def _bar(done, total, width=30):
    full = int(width * done / max(total, 1))
    return "\u2595" + "\u2588" * full + "\u2591" * (width - full) + "\u258f"


def progress(pid, now=None):
    """Live state of a running search, with a console block ready to show, or None."""
    now = now or time.time()
    g = _progress.get(pid)
    if not g or now - g["updated"] > 600:
        return None
    done, total = g["done"], g["total"]
    elapsed = max(now - g["started"], 1e-9)
    rate = done / elapsed * 60
    left = (total - done) / rate if rate > 0 else None
    t = f"T+{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}"
    head = f"KEPLER \u00b7 FIELD {pid} \u00b7 TESS SECTOR {g.get('sector', '')}".ljust(54) + t.rjust(12)
    lines = [head, RULE]
    if done == 0:
        lines += [" PHASE    ACQUISITION",
                  f" HUB      target list received \u00b7 {total} {g.get('pop', '')} stars".rstrip(),
                  " ARCHIVE  opening link to NASA MAST \u00b7 TESS full-frame images",
                  " SEARCH   workers starting \u00b7 first light curve in about 20 s"]
    else:
        lines += [" PHASE    DOWNLINK + SEARCH",
                  f" {_bar(done, total)} {100 * done // max(total, 1):3d}%   {done}/{total} stars",
                  f" RATE     {rate:.0f} stars/min" + (f"   REMAINING  {left:.1f} min" if left is not None else "") + f"   SIGNALS  {g['signals']}"]
        lines += [f" \u00b7 {x}" for x in g["stars_seen"]]
    lines += [f" \u25C9 SIGNAL  {x.replace('signal ', '')}" for x in g["signals_seen"]]
    lines.append(RULE)
    return {"stars_done": done, "stars_total": total, "percent": 100 * done // max(total, 1), "signals_so_far": g["signals"],
            "minutes_left": round(left, 1) if left is not None else None, "stars_per_minute": round(rate, 1),
            "latest_signals": g["signals_seen"], "display": "\n".join(lines)}


def get(pid):
    with _lock, _db() as c:
        p = c.execute("select * from patches where id=?", (pid,)).fetchone()
        if not p:
            raise SkyError(f"there is no patch {pid}")
        return dict(p)


def star_of(pid, tic):
    with _lock, _db() as c:
        r = c.execute("select star from patch_stars where patch_id=? and tic=?", (pid, int(tic))).fetchone()
        return json.loads(r["star"]) if r else None


# ---------------- reporting ----------------
def summary(sector=None, now=None):
    now = now or time.time()
    with _lock, _db() as c:
        _release_expired(c, now)
        q = "select sector, camera, ccd, pop, status, count(*) n, sum(n_stars) stars, sum(n_leads) leads from patches" + (" where sector=?" if sector else "") + " group by sector, camera, ccd, pop, status"
        per = {}
        for r in c.execute(q, (sector,) if sector else ()):
            d = per.setdefault((r["sector"], r["camera"], r["ccd"], r["pop"]), {"sector": r["sector"], "camera": r["camera"], "ccd": r["ccd"], "star_type": r["pop"], "patches": 0, "searched": 0, "unopened": 0, "in_progress": 0, "stars_searched": 0, "leads_found": 0})
            d["patches"] += r["n"]
            if r["status"] == "done":
                d["searched"] += r["n"]; d["stars_searched"] += r["stars"] or 0; d["leads_found"] += r["leads"] or 0
            elif r["status"] == "unopened":
                d["unopened"] += r["n"]
            else:
                d["in_progress"] += r["n"]
        building = [dict(r) for r in c.execute("select sector, camera, ccd, pop star_type from ccds where status='building'")]
        openers = [dict(r) for r in c.execute("select opened_by handle, count(*) patches, sum(n_stars) stars, sum(n_leads) leads from patches where status='done' group by opened_by order by 2 desc limit 20")]
        dets = sorted(per.values(), key=lambda d: (d["sector"], d["camera"], d["ccd"], d["star_type"]))
        tot = {k: sum(d[k] for d in dets) for k in ("patches", "searched", "unopened", "in_progress", "stars_searched", "leads_found")}
        ready = {}
        for d in dets:
            ready[d["star_type"]] = ready.get(d["star_type"], 0) + d["unopened"]
        return {"open_sectors": OPEN_SECTORS, "patch_size": PATCH_SIZE, "totals": tot, "unopened_by_star_type": ready, "detectors": dets,
                "detectors_being_mapped": building, "openers": openers,
                "star_types": {k: v["finds"] for k, v in POPULATIONS.items()}}
