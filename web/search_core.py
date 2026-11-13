"""One star's transit search, from raw TESS cube bytes to a result row. Runs in Pyodide (browser) and in CPython.

Mirrors tesshunt.sweep_core.sweep_star using only numpy, scipy and astropy (no lightkurve, no astrocut), so the
same code can run in a visitor's browser tab or an MCP App frame. The page does the byte-range reads from NASA's
public S3 cube and hands the bytes here.

Cube layout (MAST TESS FFI cubes): ext 1 is float32 big-endian [row][col][frame][flux, err];
ext 2 is a per-frame table (TSTART, TSTOP, DQUALITY, ...).
"""
import numpy as np
from scipy.ndimage import binary_dilation, label
from scipy.signal import savgol_filter
from astropy.timeseries import BoxLeastSquares

DEFAULT_BITMASK = 175  # lightkurve's "default" quality mask: attitude tweak, safe mode, coarse/earth point, desat, manual exclude


# ---------- FITS bits ----------

def parse_header(text):
    """FITS header text -> dict (values as str/int/float) and the byte length of the header block."""
    cards = [text[i:i + 80] for i in range(0, len(text), 80)]
    out = {}
    for n, c in enumerate(cards):
        if c.startswith("END") and c[3:].strip() == "":
            return out, ((n + 1) * 80 + 2879) // 2880 * 2880
        if len(c) > 10 and c[8] == "=":
            v = c[10:].split("/")[0].strip()
            if v.startswith("'"):
                v = c[10:].split("'")[1].strip()
            else:
                try:
                    v = int(v)
                except ValueError:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
            out[c[:8].strip()] = v
    raise ValueError("no END card")


_FORM = {"A": 1, "J": 4, "D": 8, "E": 4, "I": 2, "K": 8, "L": 1, "B": 1}


def table_columns(hdr, names):
    """Byte offset and type of the requested columns of a binary table header."""
    off, cols = 0, {}
    for i in range(1, int(hdr["TFIELDS"]) + 1):
        form = str(hdr[f"TFORM{i}"]).strip()
        rep = int(form[:-1]) if len(form) > 1 else 1
        code = form[-1]
        name = str(hdr[f"TTYPE{i}"]).strip()
        if name in names:
            cols[name] = (off, code)
        off += rep * _FORM[code]
    return cols


def read_table(table_bytes, hdr):
    """TSTART, TSTOP, DQUALITY per frame from the cube's frame table."""
    w, n = int(hdr["NAXIS1"]), int(hdr["NAXIS2"])
    raw = np.frombuffer(table_bytes[: w * n], dtype=np.uint8).reshape(n, w)
    cols = table_columns(hdr, {"TSTART", "TSTOP", "DQUALITY"})
    def col(name):
        o, code = cols[name]
        dt = {"D": ">f8", "J": ">i4"}[code]
        size = 8 if code == "D" else 4
        return np.ascontiguousarray(raw[:, o:o + size]).view(dt).ravel().astype(float if code == "D" else int)
    return col("TSTART"), col("TSTOP"), col("DQUALITY")


_WCS_KEYS = ("CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CD1_1", "CD1_2", "CD2_1", "CD2_2", "A_ORDER", "B_ORDER",
             "AP_ORDER", "BP_ORDER", "A_DMAX", "B_DMAX")


def frame_wcs(table_bytes, hdr):
    """Celestial WCS of the cube, from the frame table (the middle frame with a valid solution), as astrocut does."""
    from astropy.io.fits import Header
    from astropy.wcs import WCS
    w, n = int(hdr["NAXIS1"]), int(hdr["NAXIS2"])
    raw = np.frombuffer(table_bytes[: w * n], dtype=np.uint8).reshape(n, w)
    names = [str(hdr[f"TTYPE{i}"]).strip() for i in range(1, int(hdr["TFIELDS"]) + 1)]
    sip = [k for k in names if k[:2] in ("A_", "B_") or k[:3] in ("AP_", "BP_")]
    cols = table_columns(hdr, set(_WCS_KEYS) | set(sip))
    def val(i, name):
        o, code = cols[name]
        if code == "A":
            rep = int(str(hdr[f"TFORM{names.index(name) + 1}"]).strip()[:-1] or 1)
            return bytes(raw[i, o:o + rep]).decode("ascii", "replace").strip()
        size, dt = {"D": (8, ">f8"), "J": (4, ">i4")}[code]
        return np.ascontiguousarray(raw[i, o:o + size]).view(dt)[0].item()
    for i in [n // 2] + list(range(n)):
        if val(i, "CRVAL1") != 0 and val(i, "CTYPE1"):
            h = Header()
            h["WCSAXES"] = 2
            for k in cols:
                h[k] = val(i, k)
            return WCS(h)
    raise ValueError("no frame in this cube has a WCS solution")


def star_pixel(wcs, ra, dec):
    """0-based (column, row) of a sky position on the full-frame image."""
    x, y = wcs.all_world2pix([[ra, dec]], 0)[0]
    return float(x), float(y)


def position_at(star, sector):
    """Catalogue position (epoch 2000) moved by proper motion to the middle of the Sector (tesshunt.star.position_at)."""
    pmra, pmdec = star.get("pmRA") or 0.0, star.get("pmDEC") or 0.0
    years = (2018.56 + (int(sector) - 1) * 27.4 / 365.25) - 2000.0
    dec = star["dec"] + pmdec * years / 3.6e6
    ra = star["ra"] + pmra * years / 3.6e6 / max(np.cos(np.radians(star["dec"])), 1e-6)
    return ra, dec


def strips_to_cube(strips, n_frames, size):
    """Row strips (each `size` pixels x n_frames x [flux, err], float32 BE) -> flux, err arrays [frame, row, col]."""
    a = np.stack([np.frombuffer(s, dtype=">f4").reshape(size, n_frames, 2) for s in strips])  # [row, col, frame, 2]
    a = a.astype(np.float64)
    return a[..., 0].transpose(2, 0, 1), a[..., 1].transpose(2, 0, 1)


# ---------- pixels -> light curve (lightcurve_from_cutout) ----------

def threshold_aperture(flux, threshold=5.0):
    """Pixels brighter than `threshold` robust sigma above the median image, connected to the centre pixel."""
    img = np.nanmedian(flux, axis=0)
    med = np.nanmedian(img)
    mad = 1.4826 * np.nanmedian(np.abs(img - med))
    above = img > med + threshold * mad
    lab, _ = label(above)
    c = img.shape[0] // 2
    if lab[c, c] == 0:
        return np.zeros_like(above)
    return lab == lab[c, c]


def background_pixels(aper, clear_px=2, min_pixels=30):
    for grow in range(clear_px, 0, -1):
        mask = ~binary_dilation(aper, iterations=grow)
        if mask.sum() >= min_pixels:
            return mask
    return ~aper


def regress_background(raw, raw_err, regressors, n_pca=5):
    """Top n_pca patterns of the background pixels plus a constant, fitted and removed (RegressionCorrector)."""
    X = regressors - regressors.mean(axis=0)
    U, S, Vt = np.linalg.svd(X, full_matrices=False)
    dm = np.column_stack([U[:, :n_pca], np.ones(len(raw))])
    w = 1.0 / np.where(raw_err > 0, raw_err, np.nanmedian(raw_err)) ** 2
    A = dm * np.sqrt(w)[:, None]
    coef, *_ = np.linalg.lstsq(A, raw * np.sqrt(w), rcond=None)
    model = dm @ coef
    return raw - model + np.nanmedian(model)


def central_aperture(shape, half=1):
    a = np.zeros(shape, dtype=bool)
    c = shape[0] // 2
    a[c - half:c + half + 1, c - half:c + half + 1] = True
    return a


def light_curve(time, flux, err, quality):
    """Aperture photometry with background regression. Returns time, flux, flux_err, aperture."""
    good_q = (quality & DEFAULT_BITMASK) == 0
    time, flux, err = time[good_q], flux[good_q], err[good_q]
    aper = threshold_aperture(flux, 5.0)
    if aper.sum() == 0 or aper.sum() > 0.5 * aper.size:
        aper = central_aperture(aper.shape)
        bg = ~aper
    else:
        bg = background_pixels(aper)
    raw = flux[:, aper].sum(axis=1)
    raw_err = np.sqrt((err[:, aper] ** 2).sum(axis=1))
    reg = flux[:, bg]
    good = np.isfinite(raw) & np.isfinite(reg).all(axis=1) & np.isfinite(time)
    corr = regress_background(raw[good], raw_err[good], reg[good])
    return time[good], corr, raw_err[good], aper


# ---------- clean, flatten, mask (pipeline.clean / flatten / mask_jitter / drop_isolated_glitches) ----------

def clean(t, f, e, sigma_clip=5.0):
    ok = np.isfinite(f)
    t, f, e = t[ok], f[ok], e[ok]
    med = np.nanmedian(f)
    f, e = f / med, e / med
    m = np.nanmedian(f)
    mad = np.nanmedian(np.abs(f - m)) * 1.4826
    keep = f < m + sigma_clip * mad
    return t[keep], f[keep], e[keep]


def _segments(t, break_tolerance=5):
    dt = np.diff(t)
    cad = np.nanmedian(dt)
    cuts = np.where(dt > break_tolerance * cad)[0] + 1
    return np.split(np.arange(len(t)), cuts)


def flatten(t, f, e, window_hours=33.0, polyorder=2, niters=3, sigma=3.0):
    """Savitzky-Golay detrend per data segment with iterative outlier masking, as lightkurve's flatten."""
    cad = np.nanmedian(np.diff(t))
    window = int(round(window_hours / 24.0 / cad))
    window += window % 2 == 0
    window = max(window, 5)
    trend = np.empty_like(f)
    for seg in _segments(t):
        fs, ts = f[seg], t[seg]
        w = min(window, len(seg) - (1 - len(seg) % 2))
        if w <= polyorder + 1:
            trend[seg] = np.nanmedian(fs)
            continue
        mask = np.ones(len(seg), dtype=bool)
        for _ in range(niters):
            filled = np.interp(ts, ts[mask], fs[mask])
            tr = savgol_filter(filled, w, polyorder, mode="interp")
            res = fs - tr
            mask = np.abs(res) < sigma * np.nanstd(res[mask])
        trend[seg] = tr
    return f / trend, e / trend


def mask_jitter(t, f, e, window_hours=6.0, factor=3.0):
    jump = np.abs(np.diff(f, prepend=f[0]))
    cad = np.nanmedian(np.diff(t))
    w = max(int(round(window_hours / 24.0 / cad)), 3)
    half = w // 2
    local = np.array([np.nanmedian(jump[max(0, i - half): i + half + 1]) for i in range(len(f))])
    keep = local < factor * np.nanmedian(local)
    return t[keep], f[keep], e[keep]


def drop_isolated_glitches(t, f, e, nsigma=5.0, max_glitches=2):
    low = f < 1 - nsigma * np.nanstd(f)
    iso = low & ~np.roll(low, 1) & ~np.roll(low, -1)
    if iso.sum() == 0 or iso.sum() > max_glitches:
        return t, f, e
    return t[~iso], f[~iso], e[~iso]


# ---------- search (pipeline.search) ----------

def search(t, f, e, min_period=0.3, max_period=13.0, n_periods=15000,
           min_duration_hours=0.5, max_duration_hours=6.0, duration_step_hours=0.25):
    baseline = t.max() - t.min()
    max_period = min(max_period, baseline / 2.0)
    freqs = np.linspace(1.0 / max_period, 1.0 / min_period, n_periods)
    periods = np.sort(1.0 / freqs)
    durations = np.arange(min_duration_hours, max_duration_hours + 1e-9, duration_step_hours) / 24.0
    bls = BoxLeastSquares(t, f, dy=e)
    pg = bls.power(periods, durations)
    power = np.asarray(pg.power)
    med = np.nanmedian(power)
    mad = np.nanmedian(np.abs(power - med)) * 1.4826
    i = int(np.nanargmax(power))
    snr = float((power[i] - med) / mad) if mad > 0 else float("nan")
    p0, d0 = float(pg.period[i]), float(pg.duration[i]) * 24
    step = p0 * p0 * (freqs[1] - freqs[0])
    fine_p = np.linspace(max(min_period, p0 - 2 * step), min(max_period, p0 + 2 * step), 400)
    fine_d = np.arange(max(0.25, d0 - duration_step_hours), d0 + duration_step_hours + 1e-9, 0.05) / 24.0
    fine_d = fine_d[fine_d < 0.5 * fine_p.min()]
    best, j = pg, i
    if len(fine_d):
        best = bls.power(fine_p, fine_d)
        j = int(np.nanargmax(np.asarray(best.power)))
    return dict(period_d=float(best.period[j]), t0=float(best.transit_time[j]), duration_h=float(best.duration[j]) * 24,
                depth_ppm=float(best.depth[j]) * 1e6, snr=snr,
                periodogram=(periods, power))


def fold(t, f, period, t0):
    ph = ((t - t0 + 0.5 * period) % period) - 0.5 * period
    o = np.argsort(ph)
    return ph[o] * 24.0, f[o]


# ---------- vet (the parts of pipeline.vet a patch row needs) ----------

def vet_row(t, f, period, t0, duration):
    """depth_err_ppm (white noise, raised to the scatter of boxes at other phases) and the number of transits covered."""
    from_center = ((t - t0 + period / 2) % period) - period / 2
    in_tr = np.abs(from_center) < duration / 2
    out = f[~in_tr]
    scatter = np.nanstd(out)
    n_in = max(int(in_tr.sum()), 1)
    formal_err = scatter / np.sqrt(n_in / 2)
    phase = ((t - t0) % period) / period
    boxes = []
    for c in np.arange(0.15, 0.851, 0.01):
        w = np.abs(((phase - c + 0.5) % 1) - 0.5) < (duration / period) / 2
        if w.sum() >= 5:
            boxes.append(np.nanmean(out) - np.nanmean(f[w]))
    depth_err = formal_err
    if len(boxes) >= 20:
        b = np.array(boxes)
        mad = 1.4826 * np.median(np.abs(b - np.median(b)))
        if mad > 0:
            depth_err = max(formal_err, mad * np.sqrt(2))
    cyc = np.round((t - t0) / period).astype(int)
    return float(depth_err * 1e6), int(len(np.unique(cyc[in_tr])))


def max_duration_hours(period_days, density=1.0):
    return 13.0 * (period_days / 365.25) ** (1 / 3) * density ** (-1 / 3)


ROW_FIELDS = ("status", "period_d", "t0", "duration_h", "max_duration_h", "depth_ppm", "depth_err_ppm", "snr", "n_transits",
              "cadence_min", "noise_ppm")


def patch_row(star, time, flux, err, quality):
    """One star of a patch -> the row the hub expects (same fields as kepler_sweep.worker.search_one)."""
    import math
    row = {"tic": int(star["tic"]), "tmag": star.get("Tmag")}
    try:
        cad = float(np.nanmedian(np.diff(time)) * 1440)
        t, f, e, aper = light_curve(time, flux, err, quality)
        t, f, e = clean(t, f, e)
        f, e = flatten(t, f, e, window_hours=33)
        t, f, e = mask_jitter(t, f, e)
        t, f, e = drop_isolated_glitches(t, f, e)
        if len(t) < 300:
            row.update(status="too_few_cadences")
        else:
            r = search(t, f, e)
            err_ppm, n_tr = vet_row(t, f, r["period_d"], r["t0"], r["duration_h"] / 24)
            rad, mass = star.get("rad"), star.get("mass")
            dens = mass / rad ** 3 if rad and mass and rad > 0 and mass > 0 else 1.0
            row.update(status="ok", period_d=r["period_d"], t0=r["t0"], duration_h=r["duration_h"],
                       max_duration_h=max_duration_hours(r["period_d"], dens), depth_ppm=r["depth_ppm"], depth_err_ppm=err_ppm,
                       snr=r["snr"], n_transits=n_tr, cadence_min=cad, noise_ppm=float(np.nanstd(f)) * 1e6)
    except Exception as ex:
        row.update(status=f"error: {type(ex).__name__}: {str(ex)[:80]}")
    for k in ROW_FIELDS:
        v = row.get(k)
        if k != "status":
            row[k] = None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)
    return row


# ---------- one star end to end ----------

def run_star(time, flux, err, quality, progress=lambda stage, **kw: None):
    """Cube pixels for one star -> result dict. `progress(stage, **numbers)` reports each stage."""
    t, f, e, aper = light_curve(time, flux, err, quality)
    progress("curve", aperture_px=int(aper.sum()), cadences=len(t))
    t, f, e = clean(t, f, e)
    f, e = flatten(t, f, e, window_hours=33)
    t, f, e = mask_jitter(t, f, e)
    t, f, e = drop_isolated_glitches(t, f, e)
    noise = float(np.nanstd(f)) * 1e6
    progress("flat", cadences=len(t), noise_ppm=noise)
    if len(t) < 300:
        return dict(status="too_few_cadences", n_cadences=len(t))
    r = search(t, f, e)
    progress("search", period_d=r["period_d"], snr=r["snr"])
    ph_h, fph = fold(t, f, r["period_d"], r["t0"])
    pg = r.pop("periodogram")
    return dict(status="ok", aperture_px=int(aper.sum()), n_cadences=len(t), noise_ppm=noise, **r,
                fold_h=ph_h.tolist(), fold_f=fph.tolist(),
                pg_period=pg[0][::10].tolist(), pg_power=pg[1][::10].tolist())
