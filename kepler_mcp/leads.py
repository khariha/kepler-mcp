"""The leads queue and the public board: who is hunting, which lead they hold, what they concluded. No science here.

A lead is a star where a bulk search saw a repeating dip. Hunters vet leads one at a time; a lead survives only when two different
hunters call it a candidate AND the server's own test results agree, so a careless or hostile client cannot promote junk.
"""
import hashlib, json, os, re, secrets, sqlite3, threading, time

from kepler_mcp import store

CLAIM_SECONDS = 30 * 60
MAX_VETS = 3            # a lead nobody can agree on is retired after this many vets
HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,19}")
_lock = threading.RLock()
DEV = False   # true when the queue holds the development leads (known stars, for testing)

SCHEMA = """
create table if not exists leads (id integer primary key, tic integer, sector integer, period_d real, t0 real, duration_h real,
    depth_ppm real, snr real, star text, status text default 'open', created real, unique (tic, sector, period_d));
create table if not exists hunters (id_hash text primary key, handle text unique collate nocase, joined real);
create table if not exists claims (lead_id integer, hunter text, expires real, primary key (lead_id, hunter));
create table if not exists vets (lead_id integer, hunter text, verdict text, hold integer, deciding_test text, reason text,
    auto_status text, tools_seen text, ts real, primary key (lead_id, hunter));
"""


def _db():
    os.makedirs(store.DATA, exist_ok=True)
    c = sqlite3.connect(os.path.join(store.DATA, "leads.db"), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("pragma table_info(leads)")}
    for col in ("found_by", "patch_id"):
        if col not in have:
            c.execute(f"alter table leads add column {col} text")
    if "session" not in {r["name"] for r in c.execute("pragma table_info(vets)")}:
        c.execute("alter table vets add column session text")
    return c


def add(d, found_by=None, patch_id=None):
    """Queue one verified lead from a searched patch. Returns its id, or None if that signal is already queued."""
    with _lock, _db() as c:
        cur = c.execute("insert or ignore into leads (tic, sector, period_d, t0, duration_h, depth_ppm, snr, star, created, found_by, patch_id) values (?,?,?,?,?,?,?,?,?,?,?)",
                        (int(d["tic"]), int(d["sector"]), round(float(d["period_d"]), 6), round(float(d["t0"]), 5), round(float(d["duration_h"]), 2),
                         round(float(d["depth_ppm"]), 0), float(d.get("snr") or 0), json.dumps(d.get("star") or {}), time.time(), found_by, patch_id))
        return cur.lastrowid if cur.rowcount else None


def has_lead(patch_id, tic):
    """Was a lead queued for this star from this patch?"""
    with _lock, _db() as c:
        return c.execute("select 1 from leads where patch_id=? and tic=?", (patch_id, int(tic))).fetchone() is not None


def _hash(hunter_id):
    return hashlib.sha256(("kepler-hunter:" + hunter_id).encode()).hexdigest()


class LeadError(Exception):
    """A message the hunter's assistant can act on."""


# ---------------- loading ----------------
def load(path):
    """Import a leads file (JSON lines: tic, sector, period_d, t0, duration_h, depth_ppm, snr, star{...}). Existing leads are kept."""
    global DEV
    DEV = DEV or os.path.basename(path).startswith("dev_")
    n = 0
    with _lock, _db() as c:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            cur = c.execute("insert or ignore into leads (tic, sector, period_d, t0, duration_h, depth_ppm, snr, star, created) values (?,?,?,?,?,?,?,?,?)",
                            (int(d["tic"]), int(d["sector"]), round(float(d["period_d"]), 6), round(float(d["t0"]), 5), round(float(d["duration_h"]), 2),
                             round(float(d["depth_ppm"]), 0), float(d.get("snr") or 0), json.dumps(d.get("star") or {}), time.time()))
            n += cur.rowcount
            if not cur.rowcount:      # already queued: refresh the star's parameters, keep its history
                c.execute("update leads set star = ? where tic = ? and sector = ? and period_d = ?",
                          (json.dumps(d.get("star") or {}), int(d["tic"]), int(d["sector"]), round(float(d["period_d"]), 6)))
    return n


def count():
    with _lock, _db() as c:
        return c.execute("select count(*) from leads").fetchone()[0]


# ---------------- hunters ----------------
ID_SALT = os.environ.get("KEPLER_ID_SALT", "kepler-handles")


def _derive(handle, word):
    """The hunter_id for a handle and recovery word. Deterministic, so the same pair always gives the same id and nothing secret is stored."""
    import base64
    digest = hashlib.sha256(f"{ID_SALT}|{handle.lower()}|{word}".encode()).digest()
    return base64.urlsafe_b64encode(digest)[:12].decode()


WRONG_WORD_LIMIT = 5                 # wrong recovery words per handle per hour before join refuses for that handle
_wrong_words = {}


def join(handle, recovery_word):
    """Register a handle, or sign back in to one. Returns {"handle", "hunter_id", "returning"}."""
    handle = (handle or "").strip()
    word = (recovery_word or "").strip()
    if not HANDLE.fullmatch(handle):
        raise LeadError("handle must be 3 to 20 characters: letters, digits, dash or underscore, starting with a letter or digit")
    if not 4 <= len(word) <= 64:
        raise LeadError("recovery_word must be 4 to 64 characters; it is what reclaims this handle in a later session")
    hunter_id = _derive(handle, word)
    with _lock, _db() as c:
        row = c.execute("select id_hash, handle from hunters where handle = ?", (handle,)).fetchone()
        if row:
            key = handle.lower(); now = time.time()
            tries = [t for t in _wrong_words.get(key, []) if now - t < 3600]
            if len(tries) >= WRONG_WORD_LIMIT:            # checked before the word, so a guess after the limit cannot succeed
                raise LeadError(f"too many wrong recovery words for '{handle}' in the last hour; try again in about "
                                f"{max(1, int((3600 - (now - tries[0])) // 60))} min")
            if row["id_hash"] != _hash(hunter_id):
                _wrong_words[key] = tries + [now]
                raise LeadError(f"that recovery word does not match the one registered for '{handle}' (recovery words are case-sensitive). "
                                "If this is not your handle, choose a different one.")
            return {"handle": row["handle"], "hunter_id": hunter_id, "returning": True}
        c.execute("insert into hunters values (?,?,?)", (_hash(hunter_id), handle, time.time()))
    return {"handle": handle, "hunter_id": hunter_id, "returning": False}


def _handle(c, hunter_id):
    row = c.execute("select handle from hunters where id_hash = ?", (_hash(hunter_id or ""),)).fetchone()
    if not row:
        raise LeadError("unknown hunter_id; call join(handle, recovery_word) to register or to sign back in")
    return row["handle"]


STAR_TYPES = ("red", "orange", "yellow", "white")
PLANET_SIZES = ("rocky", "sub_neptune", "neptune", "giant")


def star_type(star):
    """Colour class from temperature (or from radius when the catalogue has no temperature)."""
    t, r = star.get("Teff"), star.get("rad")
    if t:
        return "red" if t < 3900 else "orange" if t < 5200 else "yellow" if t < 6000 else "white"
    if r:
        return "red" if r < 0.6 else "orange" if r < 0.85 else "yellow" if r < 1.2 else "white"
    return None


def planet_size(star, depth_ppm):
    """Size class the dip would imply if it were a planet: radius = sqrt(depth) x star radius."""
    r = star.get("rad")
    if not r or not depth_ppm or depth_ppm <= 0:
        return None, None
    re = (depth_ppm / 1e6) ** 0.5 * r * 109.1
    return ("rocky" if re < 1.8 else "sub_neptune" if re < 4 else "neptune" if re < 8 else "giant"), re


def _lead_dict(r):
    star = json.loads(r["star"] or "{}")
    size, re = planet_size(star, r["depth_ppm"])
    return {"lead_id": r["id"], "tic": r["tic"], "sector": r["sector"], "period_d": r["period_d"], "t0": r["t0"],
            "duration_h": r["duration_h"], "depth_ppm": r["depth_ppm"], "search_snr": r["snr"], "star": star,
            "found_by": r["found_by"] if "found_by" in r.keys() else None,
            "practice": bool(DEV and not (r["patch_id"] if "patch_id" in r.keys() else None)),
            "star_type": star_type(star), "size_if_planet": size, "radius_earth_if_planet": round(re, 2) if re else None}


# ---------------- queue ----------------
def _wanted(lead, want_star, want_size):
    return (not want_star or lead["star_type"] == want_star) and (not want_size or lead["size_if_planet"] == want_size)


def claim_next(hunter_id, now=None, want_star=None, want_size=None):
    """The open lead this hunter has not vetted, preferring one that already has a vet (so leads get finished).
    want_star / want_size narrow it to a star colour or an implied planet size. A held lead that does not fit is released."""
    now = now or time.time()
    with _lock, _db() as c:
        me = _handle(c, hunter_id)
        c.execute("delete from claims where expires < ?", (now,))
        mine = c.execute("select l.* from claims k join leads l on l.id = k.lead_id where k.hunter = ? and l.status = 'open'", (me,)).fetchone()
        if mine:
            if _wanted(_lead_dict(mine), want_star, want_size):
                return _lead_dict(mine) | {"resumed": True}
            c.execute("delete from claims where hunter = ?", (me,))
        rows = c.execute("""
            select l.*, (select count(*) from vets v where v.lead_id = l.id) nv,
                        (select count(*) from claims k where k.lead_id = l.id) nc
            from leads l where l.status = 'open'
              and not exists (select 1 from vets v where v.lead_id = l.id and v.hunter = ?)
            order by (l.patch_id is null), (nv + nc >= 2), nv desc, nc asc, l.snr desc, l.id""", (me,)).fetchall()
        for row in rows:
            if _wanted(_lead_dict(row), want_star, want_size):
                c.execute("insert or replace into claims values (?,?,?)", (row["id"], me, now + CLAIM_SECONDS))
                return _lead_dict(row) | {"resumed": False}
        return None


def available(hunter_id):
    """Open leads this hunter has not vetted, counted by star colour and by implied planet size."""
    with _lock, _db() as c:
        me = _handle(c, hunter_id)
        rows = c.execute("select * from leads l where l.status = 'open' and not exists (select 1 from vets v where v.lead_id = l.id and v.hunter = ?)", (me,)).fetchall()
    by_star, by_size, practice = {}, {}, 0
    for r in rows:
        d = _lead_dict(r)
        practice += d["practice"]
        by_star[d["star_type"] or "unknown"] = by_star.get(d["star_type"] or "unknown", 0) + 1
        by_size[d["size_if_planet"] or "unknown"] = by_size.get(d["size_if_planet"] or "unknown", 0) + 1
    out = {"open_for_you": len(rows), "by_star_type": by_star, "by_size_if_planet": by_size}
    if practice:
        out["of_which_practice"] = practice
    return out


def get(lead_id):
    with _lock, _db() as c:
        r = c.execute("select * from leads where id = ?", (int(lead_id),)).fetchone()
        if not r:
            raise LeadError(f"there is no lead {lead_id}")
        return _lead_dict(r) | {"status": r["status"]}


def has_real():
    """True once any lead has come from a searched patch."""
    with _lock, _db() as c:
        return c.execute("select 1 from leads where patch_id is not null limit 1").fetchone() is not None


def record(hunter_id, lead_id, verdict, hold, deciding_test, reason, auto_status, tools_seen, now=None, session=None):
    """Store one vet and settle the lead if it can be settled. Returns (lead status, summary dict)."""
    now = now or time.time()
    with _lock, _db() as c:
        me = _handle(c, hunter_id)
        lead = c.execute("select * from leads where id = ?", (int(lead_id),)).fetchone()
        if not lead:
            raise LeadError(f"there is no lead {lead_id}")
        if c.execute("select 1 from vets where lead_id = ? and hunter = ?", (lead["id"], me)).fetchone():
            raise LeadError("this handle has already vetted this lead; call next_lead")
        if lead["status"] != "open":
            raise LeadError(f"lead {lead_id} is already settled ({lead['status']}); call next_lead")
        if verdict == "candidate" and session and c.execute(
                "select 1 from vets where lead_id = ? and session = ? and verdict = 'candidate'", (lead["id"], session)).fetchone():
            raise LeadError("a candidate verdict on this signal already came from this connection under another handle; "
                            "two candidate verdicts must come from two different people")
        c.execute("insert into vets (lead_id, hunter, verdict, hold, deciding_test, reason, auto_status, tools_seen, ts, session) values (?,?,?,?,?,?,?,?,?,?)",
                  (lead["id"], me, verdict, int(bool(hold)), deciding_test, (reason or "")[:600], auto_status, json.dumps(tools_seen or []), now, session))
        c.execute("delete from claims where lead_id = ? and hunter = ?", (lead["id"], me))
        vets = c.execute("select * from vets where lead_id = ?", (lead["id"],)).fetchall()
        yes = [v for v in vets if v["verdict"] == "candidate" and not v["hold"]]
        people = {v["session"] or v["hunter"] for v in yes}       # one connection counts once, whatever handles it uses
        server_yes = auto_status == "candidate_like"
        if len(yes) >= 2 and len(people) >= 2 and server_yes:
            status = "surviving"
        elif not server_yes and verdict != "candidate":
            status = "cleared"          # hunter and server agree it is not a candidate: one vet is enough
        elif len(vets) >= MAX_VETS:
            status = "cleared"
        else:
            status = "open"
        c.execute("update leads set status = ? where id = ?", (status, lead["id"]))
        return status, {"vets_on_this_lead": len(vets), "candidate_votes": len(yes), "server_agrees_candidate": server_yes}


# ---------------- reporting ----------------
def stats(hunter_id):
    with _lock, _db() as c:
        me = _handle(c, hunter_id)
        rows = c.execute("select v.verdict, l.status from vets v join leads l on l.id = v.lead_id where v.hunter = ?", (me,)).fetchall()
        mix = {}
        for r in rows:
            mix[r["verdict"]] = mix.get(r["verdict"], 0) + 1
        found = c.execute("select count(*) from leads where found_by = ?", (me,)).fetchone()[0]
        out = {"handle": me, "leads_vetted": len(rows), "verdicts": mix,
               "on_surviving_leads": sum(1 for r in rows if r["status"] == "surviving" and r["verdict"] == "candidate")}
        if found:
            out["leads_found_by_your_sweeps"] = found
        return out


def handles():
    with _lock, _db() as c:
        return [r["handle"] for r in c.execute("select handle from hunters")]


def board(limit=50):
    with _lock, _db() as c:
        tot = {r["status"]: r["n"] for r in c.execute("select status, count(*) n from leads group by status")}
        vetted = c.execute("select count(distinct lead_id) from vets").fetchone()[0]
        surviving = []
        for l in c.execute("select * from leads where status = 'surviving' order by id desc limit ?", (limit,)):
            who = [v["hunter"] for v in c.execute("select hunter from vets where lead_id = ? and verdict = 'candidate' order by ts", (l["id"],))]
            surviving.append({"lead_id": l["id"], "tic": l["tic"], "period_d": l["period_d"], "depth_ppm": l["depth_ppm"], "found_by": l["found_by"], "vetted_by": who})
        pending = []
        for l in c.execute("""select l.* from leads l where l.status = 'open' and exists
                              (select 1 from vets v where v.lead_id = l.id and v.verdict = 'candidate') order by l.id desc limit ?""", (limit,)):
            who = [v["hunter"] for v in c.execute("select hunter from vets where lead_id = ? and verdict = 'candidate'", (l["id"],))]
            pending.append({"lead_id": l["id"], "tic": l["tic"], "period_d": l["period_d"], "depth_ppm": l["depth_ppm"], "first_vet_by": who})
        top = [dict(r) for r in c.execute("select hunter handle, count(*) leads_vetted from vets group by hunter order by 2 desc, 1 limit 20")]
        return {"leads_total": sum(tot.values()), "leads_open": tot.get("open", 0), "leads_cleared": tot.get("cleared", 0),
                "leads_surviving": tot.get("surviving", 0), "leads_with_at_least_one_vet": vetted,
                "hunters": c.execute("select count(*) from hunters").fetchone()[0],
                "surviving": surviving, "awaiting_second_vet": pending, "most_active": top,
                "note": ("DEVELOPMENT DATA: these leads are well-known stars used to test the hunting loop, not real leads. " if DEV else "") +
                        "Surviving leads are unconfirmed planet candidates found by volunteer planet hunters. Nothing here has been confirmed or submitted to any archive."}
