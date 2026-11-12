"""The leads queue and the public board: who is hunting, which lead they hold, what they concluded. No science here.

A lead is a star where a bulk search saw a repeating dip. Hunters vet leads one at a time; a lead survives only when two different
hunters call it a candidate AND the server's own test results agree, so a careless or hostile client cannot promote junk.
"""
import hashlib, json, os, re, secrets, sqlite3, threading, time

from tesshunt_mcp import store

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
    return c


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
    return n


def count():
    with _lock, _db() as c:
        return c.execute("select count(*) from leads").fetchone()[0]


# ---------------- hunters ----------------
def join(handle):
    handle = (handle or "").strip()
    if not HANDLE.fullmatch(handle):
        raise LeadError("a hunter name is 3 to 20 characters: letters, digits, dash or underscore, starting with a letter or digit")
    hunter_id = secrets.token_urlsafe(9)
    with _lock, _db() as c:
        if c.execute("select 1 from hunters where handle = ?", (handle,)).fetchone():
            raise LeadError(f"the name '{handle}' is taken. Pick another, or if it is yours, use the hunter_id you were given when you joined")
        c.execute("insert into hunters values (?,?,?)", (_hash(hunter_id), handle, time.time()))
    return {"handle": handle, "hunter_id": hunter_id}


def _handle(c, hunter_id):
    row = c.execute("select handle from hunters where id_hash = ?", (_hash(hunter_id or ""),)).fetchone()
    if not row:
        raise LeadError("unknown hunter_id. Call join(handle) first, or pass the hunter_id you were given when you joined")
    return row["handle"]


def _lead_dict(r):
    return {"lead_id": r["id"], "tic": r["tic"], "sector": r["sector"], "period_d": r["period_d"], "t0": r["t0"],
            "duration_h": r["duration_h"], "depth_ppm": r["depth_ppm"], "search_snr": r["snr"], "star": json.loads(r["star"] or "{}")}


# ---------------- queue ----------------
def claim_next(hunter_id, now=None):
    """The open lead this hunter has not vetted, preferring one that already has a vet (so leads get finished)."""
    now = now or time.time()
    with _lock, _db() as c:
        me = _handle(c, hunter_id)
        c.execute("delete from claims where expires < ?", (now,))
        mine = c.execute("select l.* from claims k join leads l on l.id = k.lead_id where k.hunter = ? and l.status = 'open'", (me,)).fetchone()
        if mine:
            return _lead_dict(mine) | {"resumed": True}
        row = c.execute("""
            select l.*, (select count(*) from vets v where v.lead_id = l.id) nv,
                        (select count(*) from claims k where k.lead_id = l.id) nc
            from leads l where l.status = 'open'
              and not exists (select 1 from vets v where v.lead_id = l.id and v.hunter = ?)
            order by (nv + nc >= 2), nv desc, nc asc, l.snr desc, l.id limit 1""", (me,)).fetchone()
        if not row:
            return None
        c.execute("insert or replace into claims values (?,?,?)", (row["id"], me, now + CLAIM_SECONDS))
        return _lead_dict(row) | {"resumed": False}


def get(lead_id):
    with _lock, _db() as c:
        r = c.execute("select * from leads where id = ?", (int(lead_id),)).fetchone()
        if not r:
            raise LeadError(f"there is no lead {lead_id}")
        return _lead_dict(r) | {"status": r["status"]}


def record(hunter_id, lead_id, verdict, hold, deciding_test, reason, auto_status, tools_seen, now=None):
    """Store one vet and settle the lead if it can be settled. Returns (lead status, summary dict)."""
    now = now or time.time()
    with _lock, _db() as c:
        me = _handle(c, hunter_id)
        lead = c.execute("select * from leads where id = ?", (int(lead_id),)).fetchone()
        if not lead:
            raise LeadError(f"there is no lead {lead_id}")
        if c.execute("select 1 from vets where lead_id = ? and hunter = ?", (lead["id"], me)).fetchone():
            raise LeadError("you have already vetted this lead; call next_lead for a new one")
        if lead["status"] != "open":
            raise LeadError(f"lead {lead_id} is already settled ({lead['status']}); call next_lead for a new one")
        c.execute("insert into vets values (?,?,?,?,?,?,?,?,?)",
                  (lead["id"], me, verdict, int(bool(hold)), deciding_test, (reason or "")[:600], auto_status, json.dumps(tools_seen or []), now))
        c.execute("delete from claims where lead_id = ? and hunter = ?", (lead["id"], me))
        vets = c.execute("select * from vets where lead_id = ?", (lead["id"],)).fetchall()
        yes = [v for v in vets if v["verdict"] == "candidate" and not v["hold"]]
        server_yes = auto_status == "candidate_like"
        if len(yes) >= 2 and server_yes:
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
        return {"handle": me, "leads_vetted": len(rows), "verdicts": mix,
                "on_surviving_leads": sum(1 for r in rows if r["status"] == "surviving" and r["verdict"] == "candidate")}


def board(limit=50):
    with _lock, _db() as c:
        tot = {r["status"]: r["n"] for r in c.execute("select status, count(*) n from leads group by status")}
        vetted = c.execute("select count(distinct lead_id) from vets").fetchone()[0]
        surviving = []
        for l in c.execute("select * from leads where status = 'surviving' order by id desc limit ?", (limit,)):
            who = [v["hunter"] for v in c.execute("select hunter from vets where lead_id = ? and verdict = 'candidate' order by ts", (l["id"],))]
            surviving.append({"lead_id": l["id"], "tic": l["tic"], "period_d": l["period_d"], "depth_ppm": l["depth_ppm"], "vetted_by": who})
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
                        "Surviving leads are unconfirmed planet candidates found by volunteers. Nothing here has been confirmed or submitted to any archive."}
