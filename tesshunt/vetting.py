"""Importable vetting pieces: catalog lookups, Gaia neighbours, and the on-target verdict.

Lifted unchanged from scripts/vet_candidates.py so the CLI, the harness and the MCP server share one copy.
"""
import io
import numpy as np
import pandas as pd
import requests

TAP = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"

def tap(q):
    r = requests.get(TAP, params={"query": q, "format": "csv"}, timeout=180); r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))

def catalog_check(tic):
    toi = tap(f"select toi, tfopwg_disp, pl_orbper, pl_trandep from toi where tid = {tic}")
    try:
        ps = tap(f"select pl_name, discoverymethod, disc_year from ps where tic_id = 'TIC {tic}'")
    except Exception:
        ps = pd.DataFrame()
    return toi, ps

def gaia_neighbors(ra, dec, tpf, radius_arcsec=45):
    from astroquery.gaia import Gaia
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    Gaia.ROW_LIMIT = 50
    g = Gaia.cone_search_async(SkyCoord(ra, dec, unit="deg"), radius=radius_arcsec * u.arcsec).get_results().to_pandas()
    px, py = tpf.wcs.all_world2pix(g.ra.values, g.dec.values, 0)
    tx, ty = tpf.wcs.all_world2pix([ra], [dec], 0)
    g["dx_px"], g["dy_px"] = px - float(tx[0]), py - float(ty[0])
    g["sep_px"] = np.hypot(g.dx_px, g.dy_px)
    g["sep_arcsec"] = g.sep_px * 21.0
    g = g.sort_values("sep_px").reset_index(drop=True)
    target_G = g.loc[0, "phot_g_mean_mag"]
    g["light_frac"] = 10 ** (-0.4 * (g.phot_g_mean_mag - target_G))   # relative to the target, G band
    return g[["source_id", "phot_g_mean_mag", "parallax", "dx_px", "dy_px", "sep_px", "sep_arcsec", "light_frac"]], (float(tx[0]), float(ty[0]))

def verdict(row, c, g):
    reasons = []
    depth = row.depth_ppm / 1e6
    near = g[(g.sep_px > 0.2) & (g.sep_px <= 2.0)]
    shift_ok = np.isfinite(c["shift_sigma"]) and c["shift_sigma"] < 3
    peak_ok = np.isfinite(c["diff_peak_dpx"]) and c["diff_peak_dpx"] <= 0.75
    # expected centroid shift if the dip is on the target and neighbors stay constant
    exp_shift = float((depth * (near.light_frac * near.sep_px).sum()) / (1 + near.light_frac.sum())) if len(near) else 0.0
    unresolved = g[(g.sep_px <= 0.2) & (g.index > 0)]
    if len(unresolved) and unresolved.light_frac.max() > 0.1:
        reasons.append(f"unresolved companion within 0.2 px ({unresolved.light_frac.max()*100:.0f}% of light): TESS cannot tell which star dips")
    capable = near[(depth / near.light_frac) < 0.6] if len(near) else near
    if not peak_ok and c["diff_peak_snr"] > 4:
        reasons.append(f"difference image peaks {c['diff_peak_dpx']:.1f} px off target at {c['diff_peak_snr']:.0f} sigma")
        v = "off_target"
    elif not shift_ok and c["shift_px"] > 5 * max(exp_shift, 1e-4):
        reasons.append(f"centroid shifts {c['shift_px']:.3f} px ({c['shift_sigma']:.0f} sigma), {c['shift_px']/max(exp_shift,1e-4):.0f}x what an on-target dip predicts")
        v = "anomaly"
    elif len(unresolved) and unresolved.light_frac.max() > 0.1:
        v = "blended"
    elif not np.isfinite(c["diff_peak_snr"]):
        v = "untestable"
        reasons.append("dip occupies too much of the period to define out-of-transit frames (rotator-like)")
    elif c["diff_peak_snr"] < 4 and shift_ok:
        v = "likely_on_target"
        reasons.append(f"difference image too weak to locate the dip ({c['diff_peak_snr']:.0f} sigma); centroid shift {c['shift_sigma']:.1f} sigma is consistent with on-target")
    else:
        v = "on_target"
        reasons.append(f"centroid shift {c['shift_sigma']:.1f} sigma; difference image peak {c['diff_peak_dpx']:.1f} px from target at {c['diff_peak_snr']:.0f} sigma")
    if len(capable):
        reasons.append(f"{len(capable)} neighbor(s) within 2 px bright enough to fake the dip with a <60% eclipse")
    if row.secondary_ppm > 2 * row.depth_err_ppm:
        reasons.append(f"possible secondary eclipse {row.secondary_ppm:.0f} ppm ({row.secondary_ppm/row.depth_err_ppm:.1f} sigma)")
    if row.period_d < 0.5:
        reasons.append("period under 12 h: check for a contact binary at 2x period")
    return v, reasons, exp_shift


def tic_neighbors(ra, dec, tpf, radius_arcsec=45):
    """Same table as gaia_neighbors, built from the TESS Input Catalog at MAST. Used when the ESA Gaia archive is down.
    Flux ratios come from Tmag (the TESS band) and are written to light_frac; phot_g_mean_mag holds the catalogue's Gaia G where it has one."""
    from astroquery.mast import Catalogs
    t = Catalogs.query_region(f"{ra} {dec}", radius=radius_arcsec / 3600.0, catalog="Tic").to_pandas()
    t = t[np.isfinite(t.Tmag.astype(float))].copy()
    px, py = tpf.wcs.all_world2pix(t.ra.values.astype(float), t.dec.values.astype(float), 0)
    tx, ty = tpf.wcs.all_world2pix([ra], [dec], 0)
    t["dx_px"], t["dy_px"] = px - float(tx[0]), py - float(ty[0])
    t["sep_px"] = np.hypot(t.dx_px, t.dy_px)
    t["sep_arcsec"] = t.sep_px * 21.0
    t = t.sort_values("sep_px").reset_index(drop=True)
    t["light_frac"] = 10 ** (-0.4 * (t.Tmag.astype(float) - float(t.loc[0, "Tmag"])))
    t["source_id"] = "TIC " + t.ID.astype(str)
    t["phot_g_mean_mag"] = pd.to_numeric(t.get("GAIAmag"), errors="coerce")
    t["parallax"] = pd.to_numeric(t.get("plx"), errors="coerce")
    return t[["source_id", "phot_g_mean_mag", "parallax", "dx_px", "dy_px", "sep_px", "sep_arcsec", "light_frac"]], (float(tx[0]), float(ty[0]))
