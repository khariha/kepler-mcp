"""Multi-sector recovery of one signal: search each sector on its own, then refine on the stitched baseline.

Lifted from scripts/followup.py so the CLI, the harness and the MCP server share one copy.
"""
import numpy as np
import pandas as pd
import lightkurve as lk
from tesshunt import pipeline as P
from tesshunt.sweep_core import get_tpf, _central_aperture


def observed_sectors(tic, ra, dec):
    from tess_stars2px import tess_stars2px_function_entry
    return sorted(set(int(s) for s in tess_stars2px_function_entry(tic, ra, dec)[3]))


def recover(ra, dec, sectors, P0, t00, dur0, depth0, cutout=15, loader=None, log=lambda m: None, flat_fn=None):
    """P0, t00, dur0 in days (t00 in the light curve's time system, BTJD); depth0 in ppm.

    loader(sector) -> TPF or None; defaults to the MAST S3 cubes.
    flat_fn(tpf) -> flattened light curve; defaults to the faint-star settings below.
    Returns dict(per_sector=DataFrame, ok=DataFrame, detected=DataFrame, flats=[...], joint=dict|None, stitched=lc|None).
    """
    if loader is None:
        loader = lambda s: get_tpf(ra, dec, s, cutout, "s3")
    stitched = None
    per_sector = []
    flats = []
    for s in sectors:
        try:
            tpf = loader(s)
            if tpf is None:
                per_sector.append(dict(sector=s, status="no cube")); log(f"  sector {s}: no cube"); continue
            cad_min = float(np.nanmedian(np.diff(tpf.time.value)) * 1440)
            if flat_fn is not None:
                flat = flat_fn(tpf)
            else:
                try:
                    lc, raw, aper = P.lightcurve_from_cutout(tpf, threshold=5)
                    if aper.sum() == 0 or aper.sum() > 0.5 * aper.size: raise ValueError
                except Exception:
                    lc, raw, aper = _central_aperture(tpf)
                flat = P.mask_jitter(P.flatten(P.clean(lc), 33), 6, 3)
            if len(flat) < 300:
                per_sector.append(dict(sector=s, status="too few cadences", cadence_min=cad_min)); continue
            r = P.search(flat, min_period=P0 * 0.97, max_period=P0 * 1.03, n_periods=3000,
                         min_duration_hours=0.5, max_duration_hours=max(4.0, dur0 * 24 * 2), duration_step_hours=0.25)
            v = P.vet(flat, r.period, r.t0, r.duration)
            noise = float(np.nanstd(flat.flux.value)) * 1e6
            per_sector.append(dict(sector=s, status="ok", cadence_min=cad_min, n=len(flat), noise_ppm=noise,
                                   period=r.period, t0=r.t0, duration_h=r.duration * 24, depth_ppm=r.depth * 1e6,
                                   snr=r.snr, n_transits=v["n_transits"], odd=v["odd_depth_ppm"], even=v["even_depth_ppm"],
                                   err=v["depth_err_ppm"], secondary=v["secondary_depth_ppm"]))
            flat.meta["SECTOR"] = s
            flats.append(flat)
            log(f"  sector {s} ({cad_min:.0f}-min): P={r.period:.4f} depth={r.depth*1e6:.0f} ppm snr={r.snr:.1f} transits={v['n_transits']}")
        except Exception as e:
            per_sector.append(dict(sector=s, status=f"error: {type(e).__name__}: {str(e)[:60]}")); log(f"  sector {s}: {type(e).__name__}: {e}")

    ps = pd.DataFrame(per_sector)
    ok = ps[ps.status == "ok"] if "status" in ps else ps.iloc[0:0]
    detected = ok[(ok.snr >= 7) & (np.abs(ok.period - P0) / P0 < 0.02) & (ok.depth_ppm > 0.4 * depth0)]
    log(f"signal recovered in {len(detected)} of {len(ok)} usable sectors")

    # ---- joint refine ----
    joint = None
    if len(flats) >= 2:
        stitched = lk.LightCurveCollection(flats).stitch()
        stitched = stitched[np.isfinite(stitched.flux.value)]
        base = stitched.time.max().value - stitched.time.min().value
        periods = np.linspace(P0 * 0.995, P0 * 1.005, 40000)
        pg = stitched.to_periodogram(method="bls", period=periods, duration=np.arange(0.5, max(4.0, dur0 * 48) + 1e-9, 0.25) / 24)
        Pj, t0j, durj = float(pg.period_at_max_power.value), float(pg.transit_time_at_max_power.value), float(pg.duration_at_max_power.value)
        dj = pg.depth_at_max_power; dj = float(dj.value) if hasattr(dj, "value") else float(dj)
        vj = P.vet(stitched, Pj, t0j, durj)
        joint = dict(period=Pj, t0=t0j, duration_h=durj * 24, depth_ppm=dj * 1e6, baseline_d=base, n_transits=vj["n_transits"],
                     odd=vj["odd_depth_ppm"], even=vj["even_depth_ppm"], err=vj["depth_err_ppm"], secondary=vj["secondary_depth_ppm"],
                     period_err_est=P0 * durj / base)
        joint["secondary_any_phase_ppm"] = vj.get("secondary_any_phase_ppm", float("nan")); joint["secondary_any_phase_at"] = vj.get("secondary_any_phase_at", float("nan"))
        joint["secondary_any_phase_sigma"] = vj.get("secondary_any_phase_sigma", float("nan")); joint["has_secondary"] = bool(vj.get("has_secondary", False))
        log(f"joint: P={Pj:.6f} d (+/-~{joint['period_err_est']:.6f}) depth={dj*1e6:.0f} ppm over {base:.0f} days, {vj['n_transits']} transits, odd/even {vj['odd_depth_ppm']:.0f}/{vj['even_depth_ppm']:.0f} +/- {vj['depth_err_ppm']:.0f}, secondary at 0.5 {vj['secondary_depth_ppm']:.0f}, deepest at any phase {joint['secondary_any_phase_ppm']:.0f} ppm (phase {joint['secondary_any_phase_at']:.2f})")
        if np.isfinite(joint["secondary_any_phase_ppm"]) and joint["secondary_any_phase_ppm"] > 4 * vj["depth_err_ppm"]:
            log("WARNING: secondary eclipse away from phase 0.5 -> likely eccentric eclipsing binary")
    return dict(per_sector=ps, ok=ok, detected=detected, flats=flats, joint=joint, stitched=stitched)
