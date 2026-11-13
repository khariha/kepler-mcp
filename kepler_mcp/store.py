"""Disk cache, trace log, rate limiter and job table. No science here."""
import collections, hashlib, json, math, os, threading, time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("KEPLER_DATA", os.path.join(ROOT, "data", "mcp"))
VERSION = "6"   # bump to invalidate cached results when the science changes


def clean(o, sig=12):
    """Make o JSON-safe: numpy scalars to Python, NaN/inf to None, floats rounded to `sig` significant figures
    (12 by default: a BJD epoch needs 12 to keep five decimals)."""
    import numpy as np
    if isinstance(o, dict):
        return {str(k): clean(v, sig) for k, v in o.items()}
    if isinstance(o, (list, tuple, np.ndarray)):
        return [clean(v, sig) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        if not math.isfinite(f):
            return None
        return float(f"{f:.{sig}g}")
    return o


def key(tool, args):
    blob = json.dumps({"v": VERSION, "tool": tool, "args": clean(args, 12)}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


def _path(tool, k, ext):
    d = os.path.join(DATA, "cache", tool)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{k}.{ext}")


def cache_get(tool, k):
    p = _path(tool, k, "json")
    if not os.path.exists(p):
        return None
    result = json.load(open(p))
    png = _path(tool, k, "png")
    return result, (open(png, "rb").read() if os.path.exists(png) else None)


def cache_find(tool, match):
    """Cached results of a tool for which match(result) is true, newest first. Used to accept evidence that was produced
    with slightly different arguments than the ones on record."""
    d = os.path.join(DATA, "cache", tool)
    if not os.path.isdir(d):
        return []
    hits = []
    for name in os.listdir(d):
        if name.endswith(".json"):
            p = os.path.join(d, name)
            try:
                r = json.load(open(p))
                if match(r):
                    hits.append((os.path.getmtime(p), r))
            except Exception:
                continue
    return [r for _, r in sorted(hits, key=lambda x: -x[0])]


def cache_put(tool, k, result, png=None):
    p = _path(tool, k, "json")
    tmp = p + ".tmp"
    json.dump(result, open(tmp, "w"))
    os.replace(tmp, p)
    if png:
        open(_path(tool, k, "png"), "wb").write(png)


# ---------------- traces ----------------
_trace_lock = threading.Lock()
_seen = collections.defaultdict(lambda: collections.deque(maxlen=30))   # (session, tic) -> [(tool, result_sha1)]


def seen(sid, tic):
    """What this session has looked at for this star so far: links a verdict to the evidence behind it."""
    return [list(x) for x in _seen.get((sid, int(tic)), [])]


def session_id(headers):
    """Anonymous, non-reversible, rotates daily. The address itself is never written anywhere."""
    h = headers or {}
    ip = (h.get("x-forwarded-for") or h.get("x-real-ip") or "local").split(",")[0].strip()
    salt = os.environ.get("KEPLER_SALT", "kepler") + time.strftime("%Y-%m-%d", time.gmtime())
    return hashlib.sha256((salt + ip + h.get("user-agent", "")).encode()).hexdigest()[:12]


def trace(sid, tool, args, result, seconds, cached, error=None):
    if os.environ.get("KEPLER_TRACES", "1") == "0":
        return
    d = os.path.join(DATA, "traces")
    os.makedirs(d, exist_ok=True)
    rec = {"ts": round(time.time(), 2), "session": sid, "tool": tool, "args": clean(args), "seconds": round(seconds, 2),
           "cached": cached, "consent": "server-notice-v1"}
    if error:
        rec["error"] = error[:200]
    else:
        body = json.dumps(result, sort_keys=True)
        rec["result_sha1"] = hashlib.sha1(body.encode()).hexdigest()[:16]
        rec["result"] = result if len(body) <= 6000 else {"truncated": True, "keys": sorted(result)[:40]}
        tic = (args or {}).get("tic") or (result.get("tic") if isinstance(result, dict) else None)
        if tic is not None and tool != "report_verdict":
            _seen[(sid, int(tic))].append((tool, rec["result_sha1"]))
    with _trace_lock, open(os.path.join(d, time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------- rate limit ----------------
LIMITS = {"light": int(os.environ.get("KEPLER_LIGHT_PER_HOUR", 60)),
          "heavy": int(os.environ.get("KEPLER_HEAVY_PER_HOUR", 5))}
_hits = collections.defaultdict(collections.deque)
_hit_lock = threading.Lock()


def allow(sid, cls, now=None):
    """Sliding one-hour window per anonymous session and cost class. Returns (ok, seconds_until_next_slot)."""
    now = now or time.time()
    with _hit_lock:
        q = _hits[(sid, cls)]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= LIMITS[cls]:
            return False, int(3600 - (now - q[0])) + 1
        q.append(now)
        return True, 0


# ---------------- jobs ----------------
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("KEPLER_JOB_WORKERS", 2)), thread_name_prefix="job")
_jobs = {}
_job_lock = threading.Lock()


def job_start(tool, k, fn):
    """Run fn() -> (result, png) in the background once per key; the key is the job id, so repeats are free."""
    with _job_lock:
        if cache_get(tool, k) is not None:
            return "done"
        j = _jobs.get(k)
        if j and j["state"] in ("queued", "running"):
            return j["state"]
        _jobs[k] = j = {"state": "queued", "tool": tool, "started": time.time(), "error": None, "progress": "", "steps": 0, "plan": None, "rows": []}

    def work():
        j["state"] = "running"
        try:
            def note(m):
                if isinstance(m, dict):                      # structured progress: a plan, or one finished row
                    if "plan" in m:
                        j["plan"] = m
                    if "row" in m:
                        j["rows"].append(m["row"]); j["steps"] += 1
                    return
                j["progress"] = str(m).strip()[:200]
            result, png = fn(note)
            cache_put(tool, k, clean(result), png)
            j["state"] = "done"
        except Exception as e:
            j["state"], j["error"] = "failed", f"{type(e).__name__}: {str(e)[:300]}"
        j["finished"] = time.time()
    _pool.submit(work)
    return "queued"


def job_get(tool, k):
    hit = cache_get(tool, k)
    if hit is not None:
        return {"state": "done"}, hit
    j = _jobs.get(k)
    if not j:
        return {"state": "unknown"}, None
    out = {"state": j["state"], "elapsed_s": round(time.time() - j["started"]), "progress": j["progress"], "steps_done": j["steps"],
           "plan": j.get("plan"), "rows": list(j.get("rows") or [])}
    if j["error"]:
        out["error"] = j["error"]
    return out, None
