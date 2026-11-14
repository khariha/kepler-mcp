"""The five moves of a transit search, as plain functions.

    clean   -> drop bad cadences, scale so "normal" = 1.0
    flatten -> remove slow drift so only fast dips remain
    search  -> try every period, score how well dips stack (BLS)
    fold    -> wrap time onto the winning period so dips overlap
    vet     -> three cheap falsification tests

Each function takes a lightkurve LightCurve and returns one, except
search (returns a dict of numbers) and vet (returns a dict of numbers).
"""
from dataclasses import dataclass
import numpy as np
import lightkurve as lk

# ---------- 1. clean ----------

def clean(lc, sigma_clip=5.0):
    """Drop NaNs and quality-flagged cadences, scale to 1.0, clip wild outliers.

    Upward outliers (cosmic rays, flares) are clipped. Downward ones are kept,
    because a transit is a downward outlier and we must not throw it away.
    """
    lc = lc.remove_nans().normalize()
    med = np.nanmedian(lc.flux.value)
    mad = np.nanmedian(np.abs(lc.flux.value - med)) * 1.4826  # robust sigma
    keep = lc.flux.value < med + sigma_clip * mad
    return lc[keep]

def mask_jitter(flat, window_hours=6.0, factor=3.0):
    """Drop stretches where the scatter is far above normal (bad pointing, glitches).

    FFI cutouts carry no quality flags, so the mask is built here. We look at the
    size of the jump between consecutive points. Inside a transit the points
    still sit close to each other (the dip is smooth; only entry and exit are
    steps), so a rolling MEDIAN of the jumps barely notices it. During bad
    pointing every consecutive pair jumps by far more than the noise, so the
    rolling median shoots up. Drop cadences where it exceeds `factor` x normal.

    Do NOT use rolling standard deviation here: for a bright, quiet star a
    transit raises local scatter several-fold and gets deleted (we hit this).
    Run this AFTER flatten.
    """
    f = flat.flux.value
    jump = np.abs(np.diff(f, prepend=f[0]))
    cadence_days = np.nanmedian(np.diff(flat.time.value))
    w = max(int(round(window_hours / 24.0 / cadence_days)), 3)
    half = w // 2
    local = np.full(len(f), np.nan)
    for i in range(len(f)):
        local[i] = np.nanmedian(jump[max(0, i - half): i + half + 1])
    typical = np.nanmedian(local)
    keep = local < factor * typical
    return flat[keep]

# ---------- 2. flatten ----------

def flatten(lc, window_hours=33.0):
    """Remove everything slower than window_hours. Transits (a few hours) pass through.

    The window must be several times longer than the longest transit you want
    to keep, or the smoother will follow the dip down and erase it.
    """
    cadence_days = np.nanmedian(np.diff(lc.time.value))
    window = int(round(window_hours / 24.0 / cadence_days))
    if window % 2 == 0:
        window += 1
    return lc.flatten(window_length=max(window, 5))

# ---------- 3. search ----------

@dataclass
class SearchResult:
    period: float          # days
    t0: float              # time of a transit center, same units as lc.time
    duration: float        # days
    depth: float           # fraction (0.00025 = 250 ppm)
    power: float           # BLS power at the peak
    snr: float             # peak power / robust scatter of the periodogram
    periodogram: object    # the lightkurve periodogram, for plotting

def search(flat, min_period=0.5, max_period=None, n_periods=20000,
           min_duration_hours=0.5, max_duration_hours=8.0, duration_step_hours=0.25):
    """Box least squares over a dense grid of periods and durations.

    max_period defaults to half the baseline, so at least two transits fit.
    snr is the peak height in units of the periodogram's own scatter; values
    above ~7-9 are conventionally worth a human look.
    """
    baseline = flat.time.max().value - flat.time.min().value
    if max_period is None:
        max_period = baseline / 2.0
    # Uniform in frequency, not in period: the phase error a grid step causes over the baseline scales as
    # baseline * dP / P^2, so a linear grid is far too coarse at short periods (LHS 3844 b folded 30 min off).
    freqs = np.linspace(1.0 / max_period, 1.0 / min_period, n_periods)
    periods = np.sort(1.0 / freqs)
    durations = np.arange(min_duration_hours, max_duration_hours + 1e-9, duration_step_hours) / 24.0
    pg = flat.to_periodogram(method="bls", period=periods, duration=durations)
    power = pg.power.value
    med = np.nanmedian(power)
    mad = np.nanmedian(np.abs(power - med)) * 1.4826
    snr = float((np.nanmax(power) - med) / mad) if mad > 0 else float("nan")
    # Refine around the best peak: a fine period grid spanning one coarse step either side, and a fine duration grid.
    p0 = float(pg.period_at_max_power.value)
    step = p0 * p0 * (freqs[1] - freqs[0]) if len(freqs) > 1 else p0 * 1e-3
    fine_p = np.linspace(max(min_period, p0 - 2 * step), min(max_period, p0 + 2 * step), 400)
    d0 = float(pg.duration_at_max_power.value) * 24
    fine_d = np.arange(max(0.25, d0 - duration_step_hours), d0 + duration_step_hours + 1e-9, 0.05) / 24.0
    fine_d = fine_d[fine_d < 0.5 * fine_p.min()]
    best = pg
    if len(fine_d):
        try:
            best = flat.to_periodogram(method="bls", period=fine_p, duration=fine_d)
        except Exception:
            best = pg
    depth = best.depth_at_max_power
    depth = float(depth.value) if hasattr(depth, "value") else float(depth)
    return SearchResult(
        period=float(best.period_at_max_power.value),
        t0=float(best.transit_time_at_max_power.value),
        duration=float(best.duration_at_max_power.value),
        depth=depth,
        power=float(pg.max_power.value) if hasattr(pg.max_power, "value") else float(pg.max_power),
        snr=snr,
        periodogram=pg,
    )

# ---------- 4. fold ----------

def fold(flat, period, t0):
    """Wrap time onto the period so every transit lands at phase 0."""
    return flat.fold(period=period, epoch_time=t0)

# ---------- 5. vet ----------

def vet(flat, period, t0, duration):
    """Three cheap tests. All are in ppm so you can compare by eye.

    odd_depth, even_depth : should agree within noise. Mismatch -> eclipsing binary at 2x period.
    secondary_depth       : dip half a period away. Nonzero -> a star, not a planet.
    depth_err             : rough 1-sigma on a single depth, for judging "agree".
    """
    t = flat.time.value
    f = flat.flux.value
    from_center = ((t - t0 + period / 2) % period) - period / 2
    in_tr = np.abs(from_center) < duration / 2
    cyc = np.round((t - t0) / period).astype(int)
    odd = f[in_tr & (cyc % 2 == 1)]
    even = f[in_tr & (cyc % 2 == 0)]
    out = f[~in_tr]
    scatter = np.nanstd(out)
    from_half = ((t - t0) % period) - period / 2
    sec = f[np.abs(from_half) < duration / 2]
    n_in = max(in_tr.sum(), 1)
    # secondary at ANY phase: an eccentric orbit moves the eclipse away from 0.5
    # (a 0.9% eclipse at phase 0.54 passes a test that only looks at phase 0.5)
    phase = ((t - t0) % period) / period
    best_any, best_phase = 0.0, np.nan
    boxes = []
    for c in np.arange(0.15, 0.851, 0.01):
        w = np.abs(((phase - c + 0.5) % 1) - 0.5) < (duration / period) / 2
        if w.sum() >= 5:
            d = np.nanmean(out) - np.nanmean(f[w])
            boxes.append(d)
            if d > best_any:
                best_any, best_phase = d, c
    # how far the deepest box stands out from all the other boxes: the scan takes a maximum over ~70 phases and
    # real light curves have correlated noise, so the white-noise error alone overstates its significance
    any_sigma = np.nan
    formal_err = scatter / np.sqrt(n_in / 2)
    depth_err = formal_err
    if len(boxes) >= 20:
        b = np.array(boxes); med = np.median(b); mad = 1.4826 * np.median(np.abs(b - med))
        if mad > 0:
            any_sigma = (best_any - med) / mad
            # a box holds all in-transit points; an odd-only or even-only depth holds half, hence sqrt(2).
            # On bright stars correlated noise dominates and the white-noise error is far too small.
            depth_err = max(formal_err, mad * np.sqrt(2))
    # a real eclipse is there every orbit; one bad stretch of data is there once
    orbits_covering, orbits_showing = 0, 0
    if np.isfinite(best_phase):
        w = np.abs(((phase - best_phase + 0.5) % 1) - 0.5) < (duration / period) / 2
        k = np.floor((t - t0) / period).astype(int)
        for c in np.unique(k[w]):
            sel = w & (k == c)
            if sel.sum() >= 3:
                orbits_covering += 1
                orbits_showing += bool(np.nanmean(out) - np.nanmean(f[sel]) >= 0.5 * best_any)
    deep = bool(np.isfinite(scatter) and n_in > 0 and best_any > 4 * formal_err and (not np.isfinite(any_sigma) or any_sigma >= 5))
    recurs = orbits_covering >= 2 and orbits_showing >= max(2, int(np.ceil(0.5 * orbits_covering)))
    return {
        "has_secondary": bool(deep and recurs),
        "secondary_single_event": bool(deep and not recurs),
        "secondary_orbits_covering": int(orbits_covering), "secondary_orbits_showing": int(orbits_showing),
        "secondary_any_phase_sigma": float(any_sigma),
        "secondary_any_phase_ppm": best_any * 1e6,
        "secondary_any_phase_at": best_phase,
        "odd_depth_ppm": (1 - np.nanmean(odd)) * 1e6 if len(odd) else float("nan"),
        "even_depth_ppm": (1 - np.nanmean(even)) * 1e6 if len(even) else float("nan"),
        "secondary_depth_ppm": (1 - np.nanmean(sec)) * 1e6 if len(sec) else float("nan"),
        "depth_err_ppm": depth_err * 1e6,
        "depth_err_white_noise_ppm": formal_err * 1e6,
        "n_transits": int(len(np.unique(cyc[in_tr]))),
    }

# ---------- physics helpers ----------

EARTH_RADII_PER_SUN = 109.1

def planet_radius_earths(depth, star_radius_suns):
    """Depth is an area ratio, so radius ratio is its square root."""
    return np.sqrt(depth) * star_radius_suns * EARTH_RADII_PER_SUN

def max_duration_hours(period_days, star_density_suns=1.0):
    """Longest possible transit (dead-center crossing, circular orbit).

    A dip much longer than this is a red flag: the star is bigger than the
    catalog says, or the dip belongs to a different, puffier star in the pixel.
    """
    return 13.0 * (period_days / 365.25) ** (1 / 3) * star_density_suns ** (-1 / 3)

# ---------- FFI pixels -> light curve ----------

def background_pixels(aper, clear_px=2, min_pixels=30):
    """Pixels used to model the background: everything at least `clear_px` pixels from the aperture.

    The star's light spills past the aperture edge, and that spill dims during a transit too. If those pixels
    are used as regressors, the background fit learns the transit and subtracts it: WASP-121 b (1.5% deep)
    came out at 943 ppm with every non-aperture pixel, and 12,331 ppm with a 2-pixel clearance.
    The clearance shrinks only if too few pixels would remain.
    """
    from scipy.ndimage import binary_dilation
    for grow in range(clear_px, 0, -1):
        mask = ~binary_dilation(aper, iterations=grow)
        if mask.sum() >= min_pixels:
            return mask
    return ~aper


def usable_regressors(reg, min_finite=0.95):
    """Background pixels that hold data in nearly every cadence. A star near the detector edge has cutout pixels off the chip,
    blank in every frame; requiring all pixels finite then left no cadences and the PCA crashed (TIC 5859202, Sector 7)."""
    keep = np.isfinite(reg).mean(axis=0) >= min_finite
    if keep.sum() < 3:
        raise ValueError("fewer than 3 background pixels hold data; the star is too close to the detector edge")
    return reg[:, keep]


def drop_isolated_glitches(flat, nsigma=5.0, max_glitches=2):
    """Drop single low cadences that look like glitches (cosmic-ray hits on the background, bad frames).

    A transit shorter than one cadence also produces isolated low points, one per orbit, so this only acts when
    there are at most `max_glitches` of them: three or more isolated dips in one Sector are far more likely a
    periodic short transit than a run of glitches (LP 791-18 c lost 2 of its 4 in-transit cadences to the old rule).
    Returns (light curve, number dropped).
    """
    f = flat.flux.value
    low = f < 1 - nsigma * np.nanstd(f)
    isolated = low & ~np.roll(low, 1) & ~np.roll(low, -1)
    if isolated.sum() == 0 or isolated.sum() > max_glitches:
        return flat, 0
    return flat[~isolated], int(isolated.sum())


def lightcurve_from_cutout(tpf, threshold=10, n_pca=5):
    """Build a light curve from a TESScut pixel cube.

    1. aperture: pixels brighter than `threshold` sigma above the cutout median,
       connected to the center pixel. Those are the star.
    2. raw light curve: sum the aperture pixels each cadence.
    3. background: the non-aperture pixels see the same scattered light and
       drift as the star. Take their top n_pca patterns and regress them out.
    Returns (corrected_lc, raw_lc, aperture_mask).
    """
    aper = tpf.create_threshold_mask(threshold=threshold, reference_pixel="center")
    raw = tpf.to_lightcurve(aperture_mask=aper)
    regressors = tpf.flux[:, background_pixels(aper)]
    regressors = regressors.value if hasattr(regressors, "value") else regressors
    # drop cadences where either the aperture sum or any background pixel is non-finite,
    # from BOTH the light curve and the regressors, so the design matrix rows line up
    regressors = usable_regressors(regressors)
    good = np.isfinite(raw.flux.value) & np.isfinite(regressors).all(axis=1)
    dm = lk.DesignMatrix(regressors[good], name="background").pca(min(n_pca, regressors.shape[1] - 1, max(int(good.sum()) - 1, 1))).append_constant()
    corrector = lk.RegressionCorrector(raw[good])
    corrected = corrector.correct(dm)
    return corrected, raw, aper

# ---------- 6. centroid test (does the dip live on the target pixel?) ----------

def _subtract_plane_background(flux):
    """Per frame: fit a + b*x + c*y to the faintest 60% of pixels and subtract it."""
    n, ny, nx = flux.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    A = np.column_stack([np.ones(ny * nx), xx.ravel(), yy.ravel()])
    out = np.empty_like(flux)
    med_img = np.nanmedian(flux, axis=0)
    faint = med_img <= np.nanpercentile(med_img, 60)
    Af = A[faint.ravel()]
    pinv = np.linalg.pinv(Af)
    for i in range(n):
        coef = pinv @ flux[i][faint]
        out[i] = flux[i] - (A @ coef).reshape(ny, nx)
    return out


def centroid_test(tpf, period, t0, duration, aperture=None, flux_floor=0.0):
    """Compare where the light is centered in-transit vs out-of-transit.

    If the dip belongs to the target, the light distribution does not move
    during transit (it only gets fainter). If it belongs to a neighbor, the
    center of light shifts away from that neighbor during the dip, and the
    difference image (out minus in) peaks on the neighbor, not the target.

    Returns dict with:
      shift_px      : distance the flux-weighted centroid moves in-transit, in pixels
      shift_sigma   : that shift divided by the scatter of out-of-transit centroids
      diff_peak_dpx : distance from the difference-image peak to the target pixel
      n_in, n_out   : cadences used
    Rule of thumb: shift_sigma < 3 and diff_peak_dpx <= 1 means "on target".
    """
    flux = tpf.flux.value if hasattr(tpf.flux, "value") else tpf.flux
    t = tpf.time.value
    good = np.isfinite(flux).all(axis=(1, 2)) & np.isfinite(t)
    flux, t = flux[good], t[good]
    ny, nx = flux.shape[1:]
    yy, xx = np.mgrid[0:ny, 0:nx]
    phase = ((t - t0 + period / 2) % period) - period / 2
    in_tr = np.abs(phase) < duration / 2
    out = (np.abs(phase) > duration) & (np.abs(phase) < 3 * duration)
    if in_tr.sum() < 3 or out.sum() < 10:
        return {"shift_px": np.nan, "shift_sigma": np.nan, "diff_peak_dpx": np.nan, "diff_peak_snr": np.nan,
                "n_in": int(in_tr.sum()), "n_out": int(out.sum()), "diff_snr_map": np.zeros((ny, nx)),
                "centroid_x": np.full(len(t), np.nan), "centroid_y": np.full(len(t), np.nan), "phase": phase, "in_transit": in_tr}
    # background-subtract each frame with a tilted plane fit to its faint pixels, so a
    # scattered-light gradient across the frame does not masquerade as a dip location
    f = _subtract_plane_background(flux)
    f = np.clip(f, flux_floor, None)
    tot = f.sum(axis=(1, 2)) + 1e-9
    cx = (f * xx).sum(axis=(1, 2)) / tot
    cy = (f * yy).sum(axis=(1, 2)) / tot
    dx = cx[in_tr].mean() - cx[out].mean()
    dy = cy[in_tr].mean() - cy[out].mean()
    shift = float(np.hypot(dx, dy))
    scatter = float(np.hypot(cx[out].std(), cy[out].std()) / np.sqrt(in_tr.sum()) + 1e-9)
    diff = f[out].mean(axis=0) - f[in_tr].mean(axis=0)      # bright where the dip happened
    # per-pixel noise of that difference, so a bright unrelated star cannot win on raw counts
    noise = np.sqrt(f[out].var(axis=0) / out.sum() + f[in_tr].var(axis=0) / in_tr.sum()) + 1e-9
    snr_map = diff / noise
    py, px = np.unravel_index(np.nanargmax(snr_map), snr_map.shape)
    ty, tx = ny // 2, nx // 2
    if aperture is not None and aperture.any():
        ty, tx = [float(v) for v in np.array(np.nonzero(aperture)).mean(axis=1)]
    return {"shift_px": shift, "shift_sigma": shift / scatter,
            "diff_peak_dpx": float(np.hypot(px - tx, py - ty)),
            "diff_peak_snr": float(snr_map[py, px]),
            "n_in": int(in_tr.sum()), "n_out": int(out.sum()), "diff_image": diff, "diff_snr_map": snr_map,
            "centroid_x": cx, "centroid_y": cy, "phase": phase, "in_transit": in_tr}
