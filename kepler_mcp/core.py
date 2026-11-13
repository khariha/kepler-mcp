"""The tools as ordinary functions: JSON-able dict in, (dict, png bytes or None) out. No MCP imports here."""
import collections, io, os, threading, time, types
import numpy as np

from tesshunt import pipeline as P
from tesshunt import cubes
from tesshunt.sweep_core import _central_aperture, candidate_bar
from kepler_mcp import store
from tesshunt.star import position_at

BRIGHT_TMAG = 9.0   # brighter than this the star's light spills past a 15-pixel cutout


def _settings(s):
    """(cutout size in pixels, aperture threshold in sigma) chosen from the star's brightness."""
    bright = s.get("Tmag") is not None and s["Tmag"] < BRIGHT_TMAG
    return (25, 10) if bright else (15, 5)


def _build_flat(tpf, threshold):
    """Pixels -> detrended, flattened light curve. Returns (flat, aperture, n_glitches_dropped)."""
    try:
        lc, raw, aper = P.lightcurve_from_cutout(tpf, threshold=threshold, n_pca=5)
        if aper.sum() == 0 or aper.sum() > 0.5 * aper.size:
            raise ValueError("bad aperture")
    except Exception:
        lc, raw, aper = _central_aperture(tpf)
    flat = P.mask_jitter(P.flatten(P.clean(lc), window_hours=33), window_hours=6, factor=3)
    # drop isolated single-cadence dips deeper than 5 sigma: a transit spans several cadences, a glitch spans one
    f = flat.flux.value
    low = f < 1 - 5 * np.nanstd(f)
    isolated = low & ~np.roll(low, 1) & ~np.roll(low, -1)
    return flat[~isolated], aper, int(isolated.sum())
MAX_FOLLOWUP_SECTORS = int(os.environ.get("KEPLER_MAX_SECTORS", 12))
TIME_NOTE = "t0 is BTJD = BJD_TDB - 2457000; fields ending _bjd_tdb are BJD_TDB"
CANDIDATE_NOTE = "a signal that passes is a planet candidate; validation or confirmation requires follow-up observations"


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90)
    _plt().close(fig)
    return buf.getvalue()


# ---------------- star + pixels ----------------
def star(query):
    """Cached TIC lookup. Returns the dict from tesshunt.star.resolve."""
    q = " ".join(str(query).split()).lower()
    k = store.key("resolve_star", {"q": q})
    hit = store.cache_get("resolve_star", k)
    if hit:
        return hit[0]
    from tesshunt.star import resolve
    out = store.clean(resolve(query), 10)
    store.cache_put("resolve_star", k, out)
    store.cache_put("resolve_star", store.key("resolve_star", {"q": f"tic {out['ID']}"}), out)
    return out


def _star_tic(tic):
    return star(f"tic {int(tic)}")


_flat_lock = threading.Lock()
_locks = collections.defaultdict(threading.RLock)   # astropy loads FITS lazily and is not safe when two threads touch one file


def _lock(tic, sector):
    with _flat_lock:
        return _locks[(int(tic), int(sector))]


def _cube(s, tic, sector, size):
    """Cutout centred on where the star was during that sector. Fast-moving stars get their own cache file."""
    ra, dec = position_at(s, sector)
    moved = (((ra - s["ra"]) * np.cos(np.radians(s["dec"]))) ** 2 + (dec - s["dec"]) ** 2) ** 0.5 * 3600
    return cubes.load_tpf(int(tic), int(sector), ra, dec, size, tag="_pm" if moved > 4 else "")
_flats = collections.OrderedDict()


def _flat(tic, sector):
    """(star, tpf, flat, aperture, cadence_min) for one sector; small in-memory LRU so search then vet is one fetch."""
    key = (int(tic), int(sector))
    with _flat_lock:
        if key in _flats:
            _flats.move_to_end(key)
            return _flats[key]
    s = _star_tic(tic)
    if int(sector) not in s["sectors"]:
        raise ValueError(f"TIC {tic} was not observed in sector {sector}; observed sectors: {s['sectors']}")
    size, thr = _settings(s)
    with _lock(tic, sector):
        tpf = _cube(s, tic, sector, size)
        if tpf is None:
            raise LookupError(f"no full-frame-image cube available yet for sector {sector}")
        flat, aper, n_glitch = _build_flat(tpf, thr)
        _ = tpf.wcs, tpf.flux, tpf.time   # finish the lazy loading while we hold the lock
    if len(flat) < 300:
        raise ValueError(f"only {len(flat)} usable cadences in sector {sector}; too few to search")
    cad = float(np.nanmedian(np.diff(tpf.time.value)) * 1440)
    out = (s, tpf, flat, aper, cad)
    with _flat_lock:
        _flats[key] = out
        while len(_flats) > 6:
            _flats.popitem(last=False)
    return out


def _star_caveats(s):
    c = []
    tm = s.get("Tmag")
    if tm is not None and tm < 6.5:
        c.append(f"saturated star (Tmag {tm:.1f}): charge bleeds along columns; a 25-pixel cutout is used but depths can be biased")
    elif tm is not None and tm < BRIGHT_TMAG:
        c.append(f"bright star (Tmag {tm:.1f}): 25-pixel cutout and a stricter aperture are used")
    elif tm is not None and tm > 15.5:
        c.append(f"very faint star (Tmag {tm:.1f}): expect noise of 1% or more per cadence; only deep dips are detectable")
    if s.get("contratio") and s["contratio"] > 0.2:
        c.append(f"crowded field: catalogue contamination ratio {s['contratio']:.2f}; depths are diluted")
    if s.get("rad_source") or s.get("mass_source"):
        c.append("the catalogue lists no radius or mass for this star; values were estimated from its absolute K magnitude with M-dwarf relations (about 5-10% uncertain)")
    if not s.get("rad"):
        c.append("no stellar radius available: planet size is not computed and the duration check assumes the Sun's density")
    pm = ((s.get("pmRA") or 0) ** 2 + (s.get("pmDEC") or 0) ** 2) ** 0.5
    if pm > 300:
        c.append(f"fast-moving star ({pm/1000:.1f} arcsec/yr): the cutout is centred on its position at the time of each sector")
    return c


def _binned(x, y, n=120):
    order = np.argsort(x); x, y = np.asarray(x)[order], np.asarray(y)[order]
    if len(x) <= n:
        return [[float(a), float(b)] for a, b in zip(x, y)]
    edges = np.linspace(x[0], x[-1], n + 1)
    idx = np.clip(np.digitize(x, edges) - 1, 0, n - 1)
    out = []
    for i in range(n):
        m = idx == i
        if m.sum():
            out.append([float(np.mean(x[m])), float(np.nanmedian(y[m]))])
    return out


# ---------------- the result contract: summary + next_actions ----------------
# Wording follows AAS-journal and TESS follow-up usage: "transit-like signal", "planet candidate", "false positive", "false alarm",
# "recovered in N of M sectors", "consistent with". Never "discovered" or "confirmed".
SUMMARY_VET = {
    "eclipsing_binary": "Secondary eclipse recurring each orbit: eclipsing binary (false positive).",
    "not_significant": "Signal below 4 sigma against the measured scatter in this Sector.",
    "not_transit_shaped": "Not transit-like: fails the duration, duty-cycle, or odd-even depth test.",
    "on_target": "Transit-like; centroid and difference image consistent with the target; no secondary eclipse over phases 0.15-0.85. Single Sector.",
    "likely_on_target": "Transit-like; centroid consistent with the target; difference image too weak to localize the signal. Single Sector.",
    "blended": "Unresolved companion within 0.2 pixel: the source of the signal cannot be assigned.",
    "anomaly": "Centroid offset larger than an on-target signal predicts; source uncertain.",
    "off_target": "Centroid offset: the difference image places the signal on a neighbouring star (nearby eclipsing binary scenario).",
    "untestable": "Signal occupies too much of the period for a localization test.",
}
SUMMARY_FOLLOW = {
    "repeats": "Signal recovered at the stitched ephemeris with no contradicting Sector.",
    "does_not_repeat": "Not recovered: at least one Sector with the sensitivity to show the signal does not show it (false alarm).",
    "eclipsing_binary": "Signal recovered, and a secondary eclipse is seen separately in at least half the Sectors: eclipsing binary (false positive).",
    "not_significant": "Stitched signal below 5 sigma against the scatter at other phases (false alarm).",
    "inconclusive": "Inconclusive: fewer than two Sectors show the signal at 4 sigma and none contradicts it.",
    "single_sector_only": "Only one usable Sector: recovery cannot be tested.",
    "no_usable_sectors": "No Sector yielded a usable light curve.",
}
# Our verdict labels in the vocabulary of the TESS follow-up community. These are this server's labels in that style,
# not dispositions assigned by the TESS Follow-up Observing Program Working Group (TFOPWG).
DISPOSITION = {
    "candidate": ("PC", "planet candidate"),
    "eclipsing_binary": ("FP", "false positive: eclipsing binary (EB)"),
    "blend_or_off_target": ("FP", "false positive: nearby eclipsing binary (NEB) or off-target signal"),
    "systematics_or_noise": ("FA", "false alarm"),
    "known_object": ("KP", "known planet, TOI or CTOI"),
    "undecided": (None, "inconclusive"),
    "inconclusive": (None, "inconclusive"),
}
BJD_OFFSET = 2457000.0


def bjd(t_btjd):
    """BTJD to BJD_TDB, the time system manuscripts and ExoFOP take."""
    return None if t_btjd is None else round(float(t_btjd) + BJD_OFFSET, 5)


def act(tool, why, **args):
    """One entry of next_actions: a tool name, complete arguments, and a short reason."""
    return {"tool": tool, "args": store.clean(args, 12), "why": why}


def _sigargs(tic, sector, b):
    return {"tic": int(tic), "sector": int(sector), "period_d": round(float(b["period_d"]), 6), "t0": round(float(b["t0"]), 5),
            "duration_h": round(float(b["duration_h"]), 2)}


# ---------------- tools ----------------
def resolve_star(query):
    s = dict(star(query))
    d = s.pop("d", None)                      # parsecs; unlabeled it was read as light-years
    s["distance_pc"] = d
    s["distance_light_years"] = d * 3.2616 if d else None
    s["units"] = {"ra": "deg", "dec": "deg", "Teff": "K", "rad": "solar radii", "mass": "solar masses",
                  "pmRA": "mas/yr", "pmDEC": "mas/yr", "Tmag": "TESS magnitude (smaller is brighter)"}
    s["note"] = "sectors lists every sector whose footprint contains the star; the newest may not have public cubes yet"
    n = len(s["sectors"])
    tm = f"Tmag {s['Tmag']:.1f}" if s.get("Tmag") is not None else "Tmag unknown"
    s["summary"] = f"TIC {s['ID']}, {tm}. " + (f"Observed in {n} Sector(s), {s['sectors'][0]} to {s['sectors'][-1]}. Stellar parameters: TIC v8." if n else "Not observed by TESS.")
    s["next_actions"] = ([act("catalog_check", "known planets and existing candidates", tic=s["ID"]),
                          act("search_transits", "transit search, earliest sector", tic=s["ID"], sector=s["sectors"][0])] if n else [])
    return s, None


def catalog_check(tic):
    from tesshunt.vetting import catalog_check as cc
    toi, ps = cc(int(tic))
    out = {"tic": int(tic),
           "is_toi": len(toi) > 0, "toi": toi.to_dict("records"),
           "known_planet_host": len(ps) > 0, "planets": sorted(set(ps.pl_name)) if len(ps) else []}
    ctoi = _ctoi_ids()
    out["is_ctoi"] = (str(int(tic)) in ctoi) if ctoi is not None else None
    out["caveats"] = [] if ctoi is not None else ["ExoFOP community-candidate list unreachable: CTOI status not checked"]
    out["caveats"].append("eclipsing-binary catalogs are not queried here; the all-phase secondary test in vet_signal is the EB check")
    if out["known_planet_host"]:
        out["summary"] = f"Known planet host: {', '.join(out['planets'])}. A signal here is a recovery of a known planet."
    elif out["is_toi"] or out["is_ctoi"]:
        out["summary"] = "Listed as a TOI or CTOI. A signal here is not new."
    else:
        out["summary"] = "Not a known planet host, TOI, or CTOI." + ("" if ctoi is not None else " CTOI list unreachable.")
    out["next_actions"] = []
    return out, None


_ctoi = {"at": 0.0, "ids": None}


def _ctoi_ids():
    """ExoFOP CTOI list, refreshed daily; falls back to the last good copy on disk. None if never fetched."""
    path = os.path.join(store.DATA, "ctoi_tics.csv")
    if _ctoi["ids"] is not None and time.time() - _ctoi["at"] < 86400:
        return _ctoi["ids"]
    try:
        import pandas as pd, requests
        r = requests.get("https://exofop.ipac.caltech.edu/tess/download_ctoi.php?sort=ctoi&output=pipe", timeout=60)
        r.raise_for_status()
        ct = pd.read_csv(io.StringIO(r.text), sep="|")
        col = [c for c in ct.columns if c.strip().lower().startswith("tic")][0]
        ids = set(ct[col].dropna().astype(int).astype(str))
        os.makedirs(store.DATA, exist_ok=True)
        open(path, "w").write("\n".join(sorted(ids)))
    except Exception:
        ids = None
        for p in (path, os.path.join(store.ROOT, "data", "ctoi_tics.csv")):
            if os.path.exists(p):
                ids = set(open(p).read().split())
                break
    _ctoi.update(at=time.time(), ids=ids)
    return ids


def light_curve(tic, sector):
    s, tpf, flat, aper, cad = _flat(tic, sector)
    t, f = flat.time.value, flat.flux.value
    out = {"tic": int(tic), "sector": int(sector), "cadence_min": cad, "n_cadences": len(flat),
           "time_start": float(t.min()), "time_end": float(t.max()), "noise_ppm": float(np.nanstd(f)) * 1e6,
           "aperture_px": int(aper.sum()), "series_binned": _binned(t, f), "time_note": TIME_NOTE,
           "processing": "threshold aperture, PCA background regression, 33 h flatten, pointing-jitter mask, isolated single-cadence glitches dropped",
           "cutout_px": int(tpf.flux.shape[1]),
           "caveats": _star_caveats(s)}
    plt = _plt()
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.plot(t, f, ".", ms=2, color="k")
    ax.set_xlabel("time [BTJD]"); ax.set_ylabel("normalized flux")
    ax.set_title(f"TIC {int(tic)}  sector {int(sector)}  {cad:.0f}-min cadence  noise {out['noise_ppm']:.0f} ppm")
    fig.tight_layout()
    out["summary"] = (f"Sector {int(sector)}: {len(flat)} cadences at {cad:.0f} min over {out['time_end'] - out['time_start']:.1f} d, "
                      f"rms {out['noise_ppm']:.0f} ppm per cadence, {out['aperture_px']}-pixel aperture, FFI photometry.")
    out["next_actions"] = [act("search_transits", "transit search on this sector", tic=int(tic), sector=int(sector))]
    return out, _png(fig)


def _density(s):
    return (s["mass"] / s["rad"] ** 3) if (s.get("rad") and s.get("mass")) else 1.0


def search_transits(tic, sector, min_period_d=0.3, max_period_d=13.0, min_snr=9.0, min_depth_ppm=0.0):
    s, tpf, flat, aper, cad = _flat(tic, sector)
    baseline = float(flat.time.value.max() - flat.time.value.min())
    if not (0.2 <= min_period_d < max_period_d <= 30):
        raise ValueError("need 0.2 <= min_period_d < max_period_d <= 30 days")
    max_period_d = min(max_period_d, baseline)
    r = P.search(flat, min_period=min_period_d, max_period=max_period_d, n_periods=15000,
                 min_duration_hours=0.5, max_duration_hours=max(0.75, min(6.0, 0.5 * min_period_d * 24)), duration_step_hours=0.25)
    v = P.vet(flat, r.period, r.t0, r.duration)
    max_dur = P.max_duration_hours(r.period, _density(s))
    passes, fails = candidate_bar(r.snr, r.depth * 1e6, r.duration * 24, r.period, max_dur, v, min_snr=min_snr, min_depth_ppm=min_depth_ppm, cadence_h=cad / 60)
    pg = r.periodogram
    per, power = pg.period.value, pg.power.value
    peaks = []
    for i in np.argsort(power)[::-1]:
        if all(abs(per[i] - p["period_d"]) / p["period_d"] > 0.02 for p in peaks):
            peaks.append({"period_d": float(per[i]), "power": float(power[i])})
        if len(peaks) == 5:
            break
    out = {"tic": int(tic), "sector": int(sector),
           "best": {"period_d": r.period, "t0": r.t0, "duration_h": r.duration * 24, "depth_ppm": r.depth * 1e6,
                    "snr": r.snr, "n_transits": v["n_transits"], "max_physical_duration_h": max_dur,
                    "odd_depth_ppm": v["odd_depth_ppm"], "even_depth_ppm": v["even_depth_ppm"],
                    "depth_err_ppm": v["depth_err_ppm"], "secondary_at_half_phase_ppm": v["secondary_depth_ppm"],
                    "secondary_any_phase_ppm": v["secondary_any_phase_ppm"], "secondary_any_phase_at": v["secondary_any_phase_at"],
                    "secondary_any_phase_sigma": v["secondary_any_phase_sigma"], "has_secondary": v["has_secondary"],
                    "radius_earth_if_planet": P.planet_radius_earths(max(r.depth, 0), s["rad"]) if s.get("rad") else None},
           "depth_sigma": (r.depth * 1e6 / v["depth_err_ppm"]) if v["depth_err_ppm"] > 0 else None,
           "passes_candidate_bar": passes, "failed_checks": fails, "other_peaks": peaks[1:],
           "bar": {"min_snr": min_snr, "min_depth_ppm": min_depth_ppm, "also": "at least 2 transits, duration within 1.5x the physical maximum, duty cycle under 15%, no secondary at any phase, odd and even depths agree"},
           "search": f"box least squares, periods {min_period_d:g}-{max_period_d:.3g} d, durations 0.5-6 h", "noise_ppm": float(np.nanstd(flat.flux.value)) * 1e6,
           "time_note": TIME_NOTE,
           "caveats": _star_caveats(s) + ["one sector only: run follow-up before believing any signal",
                       "every light curve returns a best peak; below SNR 8 it is almost always noise or leftover systematics",
                       CANDIDATE_NOTE]}
    b = out["best"]
    b["t0_bjd_tdb"] = bjd(b["t0"])
    head = f"Strongest BLS peak: P = {b['period_d']:.4f} d, depth {b['depth_ppm']:.0f} ppm, duration {b['duration_h']:.2f} h, S/N {b['snr']:.1f}, {b['n_transits']} transit(s). "
    transit_like = b["duration_h"] / 24 <= 0.25 * b["period_d"]
    if passes:
        out["summary"] = head + "Transit-like signal: passes the detection criteria."
        out["next_actions"] = [act("vet_signal", "false-positive vetting of this signal", **_sigargs(tic, sector, b))]
    elif r.snr < 8:
        out["summary"] = head + "S/N below 8: consistent with noise."
        out["next_actions"] = []
    else:
        out["summary"] = head + "Fails the detection criteria: " + "; ".join(fails)[:240] + "."
        out["next_actions"] = [act("vet_signal", "classify the failure", **_sigargs(tic, sector, b))] if transit_like else []
    plt = _plt()
    fig, ax = plt.subplots(2, 1, figsize=(9, 6))
    ax[0].plot(per, power, lw=0.6, color="k"); ax[0].set_xlabel("period [d]"); ax[0].set_ylabel("BLS power")
    ax[0].set_title(f"TIC {int(tic)} sector {int(sector)}: best P={r.period:.4f} d  SNR={r.snr:.1f}")
    folded = P.fold(flat, r.period, r.t0)
    ax[1].plot(folded.time.value * 24, folded.flux.value, ".", ms=2, color="gray")
    fb = folded.bin(time_bin_size=max(r.duration / 8, 0.005))
    ax[1].plot(fb.time.value * 24, fb.flux.value, color="red", lw=1.8)
    ax[1].set_xlim(-2 * r.duration * 24, 2 * r.duration * 24); ax[1].set_xlabel("hours from mid-transit")
    ax[1].set_title(f"depth {r.depth*1e6:.0f} ppm  duration {r.duration*24:.1f} h  " + ("PASSES bar" if passes else "fails bar"))
    fig.tight_layout()
    return out, _png(fig)


def _neighbors(s, tpf, sector=None):
    """(table, target_x, target_y, source). Gaia DR3 from the ESA archive; the TESS Input Catalog when that is down."""
    import contextlib
    from tesshunt.vetting import gaia_neighbors, tic_neighbors
    ra, dec = position_at(s, sector) if sector is not None else (s["ra"], s["dec"])
    try:
        with contextlib.redirect_stdout(io.StringIO()):   # astroquery prints the archive's error page
            g, (tx, ty) = gaia_neighbors(ra, dec, tpf)
        return g, tx, ty, "Gaia DR3 (G band)"
    except Exception:
        g, (tx, ty) = tic_neighbors(ra, dec, tpf)
        return g, tx, ty, "TESS Input Catalog (TESS band); ESA Gaia archive was unreachable"


def _neighbor_rows(g, depth_ppm=None, within_px=3.0):
    rows = []
    for i, n in g[g.sep_px <= within_px].iterrows():
        row = {"source_id": str(n.source_id), "G_mag": n.phot_g_mean_mag, "sep_px": n.sep_px, "sep_arcsec": n.sep_arcsec,
               "flux_relative_to_target": n.light_frac, "is_target": bool(i == 0)}
        if depth_ppm and i != 0 and n.light_frac > 0:
            need = depth_ppm / 1e6 / n.light_frac
            row["eclipse_depth_needed_to_fake_dip"] = need if need <= 1 else None
            row["can_fake_dip"] = bool(need < 0.6)   # same threshold as the vetting verdict
        rows.append(row)
    return rows


def neighbors(tic, sector, depth_ppm=None):
    s, tpf, flat, aper, cad = _flat(tic, sector)
    with _lock(tic, sector):
        g, tx, ty, src = _neighbors(s, tpf, sector)
    out = {"tic": int(tic), "sector": int(sector), "pixel_arcsec": 21.0, "neighbor_source": src,
           "neighbors_within_3px": _neighbor_rows(g, depth_ppm),
           "caveats": ["with Gaia G-band flux ratios, red neighbours count for more in the TESS band than listed",
                       "stars closer than ~0.2 px (4 arcsec) cannot be separated by TESS at all"]}
    others = [n for n in out["neighbors_within_3px"] if not n["is_target"]]
    fakers = [n for n in others if n.get("can_fake_dip")]
    if not others:
        out["summary"] = "No other catalogued star within 3 pixels."
    elif depth_ppm is None:
        out["summary"] = f"{len(others)} other star(s) within 3 pixels. No depth given, so none was tested against the signal."
    elif fakers:
        out["summary"] = f"{len(others)} other star(s) within 3 pixels; {len(fakers)} bright enough to reproduce a {depth_ppm:.0f} ppm signal as a nearby eclipsing binary (eclipse under 60%)."
    else:
        out["summary"] = f"{len(others)} other star(s) within 3 pixels; none bright enough to reproduce a {depth_ppm:.0f} ppm signal."
    out["next_actions"] = []
    return out, None


def vet_signal(tic, sector, period_d, t0, duration_h):
    from tesshunt.vetting import verdict as _verdict
    s, tpf, flat, aper, cad = _flat(tic, sector)
    dur = duration_h / 24
    if not (0.1 < period_d < 30 and 0.2 <= duration_h <= 12):
        raise ValueError("period must be 0.1-30 d and duration 0.2-12 h")
    if dur > 0.25 * period_d:
        raise ValueError(f"a {duration_h:g} h dip is {100*dur/period_d:.0f}% of a {period_d:.3f} d period: rotation or pulsation, not a transit; "
                         "the transit tests do not apply above 25% of the period")
    v = P.vet(flat, period_d, t0, dur)
    t, f = flat.time.value, flat.flux.value
    in_tr = np.abs(((t - t0 + period_d / 2) % period_d) - period_d / 2) < dur / 2
    if in_tr.sum() < 3:
        raise ValueError("fewer than 3 in-transit cadences at that period, epoch and duration in this sector")
    depth_ppm = float((np.nanmean(f[~in_tr]) - np.nanmean(f[in_tr])) * 1e6)
    err = v["depth_err_ppm"]
    with _lock(tic, sector):
        c = P.centroid_test(tpf, period_d, t0, dur, aperture=aper)
        g, tx, ty, src = _neighbors(s, tpf, sector)
    row = types.SimpleNamespace(depth_ppm=depth_ppm, secondary_ppm=v["secondary_depth_ppm"], depth_err_ppm=err, period_d=period_d)
    verdict, reasons, exp_shift = _verdict(row, c, g)
    any_sec, any_ph = v["secondary_any_phase_ppm"], v["secondary_any_phase_at"]
    any_sig = v["secondary_any_phase_sigma"]
    if v["has_secondary"]:
        verdict = "eclipsing_binary"
        reasons.insert(0, f"secondary eclipse {any_sec:.0f} ppm at phase {any_ph:.2f} ({any_sec/err:.1f} sigma formal, {any_sig:.1f} sigma against the other phases): eclipsing binary, eccentric if away from 0.5")
    else:
        reasons.append(f"no secondary at any phase 0.15-0.85 (deepest box {any_sec:.0f} ppm at {any_ph:.2f}, {any_sig:.1f} sigma against the other phases)")
    max_dur = P.max_duration_hours(period_d, _density(s))
    passes, fails = candidate_bar(depth_ppm / err if err > 0 else float("nan"), depth_ppm, duration_h, period_d, max_dur, v, min_snr=0, min_depth_ppm=0, cadence_h=cad / 60)
    location = verdict
    shape_fails = [x for x in fails if not x.startswith("secondary")]
    if v.get("secondary_single_event"):
        reasons.insert(0, f"one deep event ({any_sec:.0f} ppm) at phase {any_ph:.2f}, seen in {v['secondary_orbits_showing']} of {v['secondary_orbits_covering']} orbits here: "
                          "an eclipse and a data glitch look the same in one sector, so check that phase in other sectors (followup_start does)")
    if verdict != "eclipsing_binary":
        if np.isfinite(err) and err > 0 and depth_ppm / err < 4:
            verdict = "not_significant"
            reasons.insert(0, f"the dip is only {depth_ppm/err:.1f} sigma in this sector against the measured scatter")
        elif shape_fails:
            verdict = "not_transit_shaped"
            reasons[0:0] = shape_fails
    out = {"tic": int(tic), "sector": int(sector), "verdict": verdict, "location_test": location, "reasons": reasons,
           "depth_ppm": depth_ppm, "depth_err_ppm": err, "depth_sigma": depth_ppm / err if err > 0 else None,
           "n_transits": v["n_transits"], "odd_depth_ppm": v["odd_depth_ppm"], "even_depth_ppm": v["even_depth_ppm"],
           "secondary_at_half_phase_ppm": v["secondary_depth_ppm"], "secondary_any_phase_ppm": any_sec, "secondary_any_phase_at": any_ph,
           "secondary_any_phase_sigma": any_sig, "has_secondary": v["has_secondary"],
           "secondary_scan": "box of the transit duration at every phase 0.15-0.85, step 0.01; a secondary is called when the deepest box is over 4 sigma formally and at least 5 robust sigma deeper than the other boxes",
           "duration_h": duration_h, "max_physical_duration_h": max_dur, "shape_checks_failed": fails,
           "centroid_shift_px": c["shift_px"], "centroid_shift_sigma": c["shift_sigma"], "expected_shift_if_on_target_px": exp_shift,
           "difference_image_peak_offset_px": c["diff_peak_dpx"], "difference_image_peak_sigma": c["diff_peak_snr"],
           "n_in_transit": c["n_in"], "n_out_of_transit": c["n_out"],
           "neighbor_source": src, "neighbors_within_3px": _neighbor_rows(g, depth_ppm),
           "radius_earth_if_planet": P.planet_radius_earths(max(depth_ppm, 0) / 1e6, s["rad"]) if s.get("rad") else None,
           "verdict_values": "eclipsing_binary, not_significant, not_transit_shaped, then the location result: on_target, likely_on_target, blended, anomaly, off_target, untestable",
           "caveats": _star_caveats(s) + ["one sector only: a verdict here is provisional until follow-up repeats it",
                       "an on_target verdict does not rule out a bound or background star inside the same pixel",
                       CANDIDATE_NOTE]}
    out["t0_bjd_tdb"] = bjd(t0)
    out["odd_minus_even_ppm"] = v["odd_depth_ppm"] - v["even_depth_ppm"]
    out["secondary_phase_range"] = [0.15, 0.85]
    sec = ("" if verdict == "eclipsing_binary" else
           f" Deepest feature over phases 0.15-0.85: {any_sec:.0f} ppm ({any_sig:.1f} sigma against other phases).")
    out["summary"] = (f"{verdict}. " + SUMMARY_VET.get(verdict, "") + f" Depth {depth_ppm:.0f} +/- {err / 2 ** 0.5:.0f} ppm, {v['n_transits']} transit(s), "
                      f"odd-even {out['odd_minus_even_ppm']:.0f} +/- {err * 2 ** 0.5:.0f} ppm.{sec}"
                      if err and err > 0 else f"{verdict}. " + SUMMARY_VET.get(verdict, ""))
    if verdict in ("on_target", "likely_on_target"):
        out["next_actions"] = [act("followup_start", "test repetition across sectors", tic=int(tic), period_d=round(float(period_d), 6), t0=round(float(t0), 5),
                                   duration_h=round(float(duration_h), 2), depth_ppm=round(float(depth_ppm)))]
    else:
        out["next_actions"] = []
    plt = _plt()
    fig, ax = plt.subplots(1, 3, figsize=(13, 4))
    mean = np.nanmean(tpf.flux.value, axis=0)
    ax[0].imshow(np.log10(np.clip(mean, 1, None)), origin="lower", cmap="gray")
    ax[0].contour(aper, levels=[0.5], colors="red", linewidths=0.8)
    for _, n in g[g.sep_px < 3].iterrows():
        ax[0].plot(tx + n.dx_px, ty + n.dy_px, "o", mfc="none", mec="cyan", ms=6 + 10 * min(n.light_frac, 1))
    ax[0].set_title("average image; cyan = catalogue stars")
    im = ax[1].imshow(c["diff_snr_map"], origin="lower", cmap="magma"); fig.colorbar(im, ax=ax[1], fraction=0.046)
    ax[1].contour(aper, levels=[0.5], colors="cyan", linewidths=0.8)
    ax[1].set_title(f"where the dip happened (sigma): {verdict}")
    ph = ((t - t0) / period_d + 0.25) % 1 - 0.25
    ax[2].plot(ph, f, ".", ms=2, color="gray")
    bx = _binned(ph, f, 100); ax[2].plot([p[0] for p in bx], [p[1] for p in bx], color="red", lw=1.5)
    ax[2].set_xlabel("orbital phase (transit at 0)"); ax[2].set_title("full phase curve: look for a second dip")
    fig.tight_layout()
    return out, _png(fig)


def fold_plot(tic, sectors, period_d, t0):
    sectors = [int(x) for x in sectors][:4]
    if not sectors:
        raise ValueError("give 1 to 4 sectors")
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8, 4))
    allp, allf, used = [], [], []
    for sec in sectors:
        s, tpf, flat, aper, cad = _flat(tic, sec)
        ph = (((flat.time.value - t0) / period_d + 0.5) % 1 - 0.5) * period_d * 24
        ax.plot(ph, flat.flux.value, ".", ms=2, alpha=0.5, label=f"sector {sec}")
        allp.append(ph); allf.append(flat.flux.value); used.append(sec)
    ph, f = np.concatenate(allp), np.concatenate(allf)
    win = np.abs(ph) < 8
    series = _binned(ph[win], f[win], 60)
    ax.plot([p[0] for p in series], [p[1] for p in series], color="red", lw=1.8)
    ax.set_xlim(-8, 8); ax.set_xlabel("hours from mid-transit"); ax.set_ylabel("normalized flux"); ax.legend(fontsize=8)
    ax.set_title(f"TIC {int(tic)} folded at P={period_d:.6f} d")
    fig.tight_layout()
    return {"tic": int(tic), "sectors": used, "period_d": period_d, "t0": t0, "fold_binned_hours_flux": series, "time_note": TIME_NOTE}, _png(fig)


def _box_stats(t, f, period, t0, dur):
    """Depth of the in-transit box and how far it stands out from boxes of the same width at other phases.
    Returns (depth_ppm, robust scatter of off-transit boxes in ppm, n_in). The scatter is empirical, so correlated
    noise and leftover systematics count against the signal instead of being ignored."""
    ph = ((t - t0) % period) / period
    in_tr = np.abs(((t - t0 + period / 2) % period) - period / 2) < dur / 2
    n_in = int(in_tr.sum())
    if n_in < 3:
        return None, None, n_in
    base = np.nanmean(f[~in_tr])
    depth = float((base - np.nanmean(f[in_tr])) * 1e6)
    boxes = []
    for c in np.arange(0.1, 0.901, max(dur / period, 0.01)):
        w = np.abs(((ph - c + 0.5) % 1) - 0.5) < (dur / period) / 2
        if w.sum() >= max(3, n_in // 2):
            boxes.append((base - np.nanmean(f[w])) * 1e6)
    if len(boxes) >= 8:
        b = np.array(boxes)
        scatter = float(1.4826 * np.median(np.abs(b - np.median(b))))
    else:
        scatter = float(np.nanstd(f[~in_tr]) / np.sqrt(n_in) * 1e6) * 1.5   # too few boxes: inflate the white-noise error
    return depth, max(scatter, 1e-9), n_in


def _record(star_row, joint, agree, usable, sectors):
    """One row in the shape an RNAAS table or an ExoFOP CTOI upload takes. Uncertainties here are box-fit estimates:
    a transit-model fit is required before publication."""
    depth, half_err = joint["depth_ppm"], joint["err"]
    depth_err = half_err / 2 ** 0.5                      # joint["err"] is the error of an odd-only or even-only depth
    sig = depth / depth_err if depth_err > 0 else None
    dur_h = joint["duration_h"]
    rad = star_row.get("rad")
    rp = P.planet_radius_earths(max(depth, 0) / 1e6, rad) if rad else None
    rp_err = (rp * ((0.5 * depth_err / depth) ** 2 + 0.05 ** 2) ** 0.5) if (rp and depth > 0) else None   # 5% assumed on the stellar radius
    return {
        "tic_id": int(star_row["ID"]), "tmag": star_row.get("Tmag"),
        "epoch_bjd_tdb": joint["t0_bjd_tdb"], "epoch_err_d": round(dur_h / 24 / (2 * sig), 5) if sig else None,
        "period_d": round(joint["period"], 6), "period_err_d": round(joint["period_err_est"], 6),
        "depth_ppm": round(depth), "depth_err_ppm": round(depth_err),
        "duration_h": round(dur_h, 2), "duration_err_h": 0.13,
        "rp_rearth": round(rp, 2) if rp else None, "rp_err_rearth": round(rp_err, 2) if rp_err else None,
        "snr": round(sig, 1) if sig else None, "n_transits": joint["n_transits"],
        "sectors": [int(x) for x in sectors], "recovered_sectors": [int(x) for x in agree],
        "notes": {
            "odd_minus_even_ppm": round(joint["odd"] - joint["even"]), "odd_minus_even_err_ppm": round(half_err * 2 ** 0.5),
            "secondary_limit_ppm": round(joint.get("secondary_any_phase_ppm") or 0), "secondary_phase_range": [0.15, 0.85],
            "recovered": f"{len(agree)} of {usable} Sectors",
            "stellar_parameters": "TIC v8" + (" (radius and mass estimated from absolute K magnitude)" if star_row.get("rad_source") else ""),
            "photometry": "TESS full-frame images, simple aperture, box-least-squares search",
            "uncertainties": "box-fit estimates (duration from a 0.25 h grid, 5% assumed on stellar radius); a transit-model fit is required for publication",
        },
    }


def followup(tic, period_d, t0, duration_h, depth_ppm, progress=lambda m: None):
    from tesshunt.followup import recover
    s = _star_tic(tic)
    sectors = s["sectors"]
    skipped = sectors[MAX_FOLLOWUP_SECTORS:]
    sectors = sectors[:MAX_FOLLOWUP_SECTORS]
    size, thr = _settings(s)
    if duration_h / 24 > 0.25 * period_d:
        raise ValueError(f"a {duration_h:g} h dip is {100*duration_h/24/period_d:.0f}% of a {period_d:.3f} d period: not transit-shaped, so there is nothing to follow up")

    def loader(sec):
        with _lock(tic, sec):
            return _cube(s, tic, sec, size)
    r = recover(s["ra"], s["dec"], sectors, period_d, t0, duration_h / 24, depth_ppm, cutout=size, loader=loader, log=progress,
                flat_fn=lambda tpf: _build_flat(tpf, thr)[0])
    ok, det, joint = r["ok"], r["detected"], r["joint"]
    usable, recovered = len(ok), len(det)
    # Fixed-ephemeris check: measure the depth in each sector at the stitched period and epoch. No search, so no
    # look-elsewhere effect, and a shallow signal counts even where a sector's own search could not find it alone.
    checks, agree, contradict = [], [], []
    if joint:
        Pj, tj, dj, depj = joint["period"], joint["t0"], joint["duration_h"] / 24, joint["depth_ppm"]
        for flat in r["flats"]:
            sec = int(flat.meta.get("SECTOR", -1))
            depth, err, n_in = _box_stats(flat.time.value, flat.flux.value, Pj, tj, dj)
            if depth is None:
                checks.append({"sector": sec, "result": "no_transit_covered"}); continue
            expected_sigma = depj / err
            if depth / err >= 4 and 0.5 <= depth / depj <= 2.0:
                res = "agrees"; agree.append(sec)
            elif expected_sigma >= 5 and depth < 0.4 * depj:
                res = "contradicts"; contradict.append(sec)
            else:
                res = "too_noisy_to_tell"
            checks.append({"sector": sec, "depth_ppm": depth, "box_scatter_ppm": err, "depth_sigma": depth / err,
                           "expected_sigma": expected_sigma, "n_in_transit": n_in, "result": res})
        st = r["stitched"]
        jd, je, _ = _box_stats(st.time.value, st.flux.value, Pj, tj, dj) if st is not None else (None, None, 0)
        joint["depth_sigma_empirical"] = (jd / je) if jd is not None else None
    joint_sigma = (joint or {}).get("depth_sigma_empirical") or 0.0
    if usable == 0:
        status = "no_usable_sectors"
    elif usable == 1 or not joint:
        status = "single_sector_only"
    elif contradict:
        status = "does_not_repeat"
    elif len(agree) >= 2 and joint_sigma >= 7:
        status = "repeats"
    elif joint_sigma < 5:
        status = "not_significant"
    else:
        status = "inconclusive"
    out = {"tic": int(tic), "input": {"period_d": period_d, "t0": t0, "duration_h": duration_h, "depth_ppm": depth_ppm},
           "sectors_searched": sectors, "sectors_skipped_over_limit": skipped, "usable_sectors": usable, "status": status,
           "sectors_agreeing": agree, "sectors_contradicting": contradict, "fixed_ephemeris_check": checks,
           "found_by_independent_search_in": det.sector.tolist() if recovered else [],
           "status_rule": "depths are judged against the scatter of same-width boxes at other phases, so correlated noise counts against the signal. "
                          "At the stitched period and epoch a sector agrees when its dip is over 4 sigma and within a factor 2 of the stitched depth; "
                          "it contradicts when it had the sensitivity (expected over 5 sigma) but shows under 40% of the depth. "
                          "repeats = at least 2 agree, none contradict, and the stitched dip is over 7 sigma; not_significant = stitched dip under 5 sigma",
           "per_sector": r["per_sector"].to_dict("records"), "joint": joint, "time_note": TIME_NOTE, "caveats": [CANDIDATE_NOTE]}
    if joint:
        err = joint["err"]
        if joint.get("has_secondary"):
            # A stitched light curve can show a deep off-phase box that comes from one bad stretch of one sector.
            # A real eclipse is there every orbit, so ask each sector separately, at the same phase.
            ph0, sdep = joint["secondary_any_phase_at"], joint["secondary_any_phase_ppm"]
            sec_yes, sec_cov = [], []
            for flat in r["flats"]:
                t, f = flat.time.value, flat.flux.value
                ph = ((t - tj) % Pj) / Pj
                w = np.abs(((ph - ph0 + 0.5) % 1) - 0.5) < (dj / Pj) / 2
                intr = np.abs(((t - tj + Pj / 2) % Pj) - Pj / 2) < dj / 2
                if w.sum() < 3:
                    continue
                sec_cov.append(int(flat.meta.get("SECTOR", -1)))
                e = float(np.nanstd(f[~intr]) / np.sqrt(w.sum()) * 1e6)
                d = float((np.nanmedian(f[~intr & ~w]) - np.nanmedian(f[w])) * 1e6)
                if e > 0 and d / e >= 3 and d >= 0.5 * sdep:
                    sec_yes.append(sec_cov[-1])
            need = max(2, int(np.ceil(0.5 * len(sec_cov))))
            out["secondary_check"] = {"phase": ph0, "depth_ppm_stitched": sdep, "sectors_covering": sec_cov, "sectors_showing_it": sec_yes,
                                      "rule": f"an eclipse is real when at least {need} of the covering sectors each show it at 3 sigma"}
            if len(sec_yes) >= need:
                out["secondary_check"]["conclusion"] = "a real second eclipse: eclipsing binary"
                out["status"] = "eclipsing_binary"
                out["caveats"].insert(0, f"secondary eclipse {sdep:.0f} ppm at phase {ph0:.2f}, seen separately in {len(sec_yes)} of {len(sec_cov)} sectors: eclipsing binary")
            else:
                out["secondary_check"]["conclusion"] = "not an eclipse: no single sector shows it, so it is leftover noise in the stack, not a second body"
                out["caveats"].insert(0, f"the stitched data show a {sdep:.0f} ppm dip at phase {ph0:.2f}, but only {len(sec_yes)} of {len(sec_cov)} sectors show it on their own: leftover noise in the stack, not an eclipse and not evidence of another body")
        if abs(joint["odd"] - joint["even"]) > 3 * err:
            out["caveats"].insert(0, "odd and even depths differ in the stitched data: possible eclipsing binary at twice the period")
    if status == "does_not_repeat":
        out["caveats"].insert(0, f"sector(s) {contradict} had the sensitivity to see this dip and did not: a real orbiting body transits every period")
    if status == "not_significant":
        out["caveats"].insert(0, "stitched dip under 5 sigma against the scatter at other phases: consistent with noise selected by a period search")
    if status == "inconclusive":
        out["caveats"].insert(0, "fewer than two sectors show the dip at 4 sigma and none contradicts it")
    out["summary"] = f"{out['status']}. " + SUMMARY_FOLLOW.get(out["status"], "") + f" Recovered in {len(agree)} of {usable} Sector(s); {len(contradict)} contradict."
    if joint:
        joint["t0_bjd_tdb"] = bjd(joint["t0"])
        out["summary"] += (f" Stitched ephemeris: P = {joint['period']:.6f} +/- {joint['period_err_est']:.6f} d, T0 = {joint['t0_bjd_tdb']:.5f} BJD_TDB, "
                           f"depth {joint['depth_ppm']:.0f} ppm, {joint['n_transits']} transits.")
        out["record"] = _record(s, joint, agree, usable, sectors)
    out["next_actions"] = []
    png = None
    if r["stitched"] is not None and joint:
        plt = _plt()
        st = r["stitched"]
        fig, ax = plt.subplots(figsize=(8, 4))
        ph = (((st.time.value - joint["t0"]) / joint["period"] + 0.5) % 1 - 0.5) * joint["period"] * 24
        ax.plot(ph, st.flux.value, ".", ms=2, alpha=0.3, color="gray")
        w = np.abs(ph) < 8
        b = _binned(ph[w], st.flux.value[w], 60); ax.plot([p[0] for p in b], [p[1] for p in b], color="red", lw=1.8)
        ax.set_xlim(-8, 8); ax.set_xlabel("hours from mid-transit")
        ax.set_title(f"TIC {int(tic)}: {usable} sectors stitched, P={joint['period']:.6f} d, {joint['n_transits']} transits, {out['status']}")
        fig.tight_layout(); png = _png(fig)
    return out, png
