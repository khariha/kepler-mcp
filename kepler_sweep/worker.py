"""The per-star search, in its own importable module so worker processes can load it however the helper was started."""
import os, socket, warnings
socket.setdefaulttimeout(float(os.environ.get("KEPLER_NET_TIMEOUT", 60)))

FIELDS = ("status", "period_d", "t0", "duration_h", "max_duration_h", "depth_ppm", "depth_err_ppm", "snr", "n_transits", "cadence_min", "noise_ppm")


def search_one(args):
    """Search one star. Runs in a worker process. Returns one plain row of numbers."""
    warnings.filterwarnings("ignore")
    star, sector = args
    import math, types
    from tesshunt.sweep_core import sweep_star
    t = types.SimpleNamespace(ID=star["tic"], ra=star["ra"], dec=star["dec"], Tmag=star.get("Tmag"), Teff=star.get("Teff"), rad=star.get("rad"),
                              mass=star.get("mass"), contratio=star.get("contratio") or 0.0, camera=0, ccd=0, pmRA=star.get("pmRA"), pmDEC=star.get("pmDEC"))
    row, _ = sweep_star(t, sector, min_depth_ppm=0.0, make_plot=False)
    out = {"tic": star["tic"], "tmag": star.get("Tmag")}
    for k in FIELDS:
        v = row.get(k)
        out[k] = v if k == "status" else (None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v))
    return out
