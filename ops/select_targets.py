"""Operator tool. Build the target list for one sector: stars TESS looked at that nobody has claimed.

    1. tile the sector's four camera fields with small cones
    2. pull every TIC star in those cones inside the magnitude, temperature and radius limits (defaults: faint red dwarfs)
    3. keep only stars tess-point says actually landed on a CCD in this sector
    4. remove 2-minute targets (NASA already searched them)
    5. remove TOIs, CTOIs (if ExoFOP is up), known planet hosts, and every TIC in ops/private_exclude.txt
    6. rank by brightness, write ops/data/targets_s<N>.csv

Run from the repo root:  python ops/select_targets.py --sector 7
Network only; no paid APIs.
"""
import argparse, io, os, sys, time
import numpy as np
import pandas as pd
import requests
from astropy.coordinates import SkyCoord, SkyOffsetFrame
import astropy.units as u
from astroquery.mast import Catalogs, Observations
from tess_stars2px import tess_stars2px_function_entry, TESS_Spacecraft_Pointing_Data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "ops", "data")
os.makedirs(DATA, exist_ok=True)

ap = argparse.ArgumentParser()
ap.add_argument("--sector", type=int, default=1)
ap.add_argument("--tmag", type=float, nargs=2, default=[12.0, 15.0])
ap.add_argument("--teff-min", type=float, default=2300.0)
ap.add_argument("--teff-max", type=float, default=4000.0)
ap.add_argument("--rad-max", type=float, default=0.6)
ap.add_argument("--cone-deg", type=float, default=5.5)
args = ap.parse_args()
S = args.sector

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---- 1. tile the four camera fields ----
sc = TESS_Spacecraft_Pointing_Data()
idx = list(sc.sectors).index(S)
cones = []
for cam in range(4):
    center = SkyCoord(ra=float(sc.camRa[cam][idx]) * u.deg, dec=float(sc.camDec[cam][idx]) * u.deg)
    frame = SkyOffsetFrame(origin=center)
    for dx in (-9, -3, 3, 9):
        for dy in (-9, -3, 3, 9):
            c = SkyCoord(lon=dx * u.deg, lat=dy * u.deg, frame=frame).transform_to("icrs")
            cones.append((cam + 1, c.ra.deg, c.dec.deg))
log(f"sector {S}: {len(cones)} cones of {args.cone_deg} deg over 4 cameras")

# ---- 2. pull TIC stars in each cone (cached: this step takes ~25 min) ----
pool_cache = os.path.join(DATA, f"tic_pool_s{S}_tmag{args.tmag[0]:g}-{args.tmag[1]:g}_teff{args.teff_min:g}-{args.teff_max:g}_rad{args.rad_max:g}.csv")
partial = pool_cache.replace(".csv", ".partial.csv")
frames, done = [], set()
if os.path.exists(partial) and not os.path.exists(pool_cache):
    prev = pd.read_csv(partial)
    frames.append(prev); done = set(prev["cone"].unique())
    log(f"resuming: {len(done)} cones already pulled ({len(prev)} rows)")
for i, (cam, ra, dec) in enumerate(cones if not os.path.exists(pool_cache) else []):
    if i in done:
        continue
    for attempt in range(3):
        try:
            tab = Catalogs.query_criteria(catalog="Tic", coordinates=f"{ra} {dec}", radius=args.cone_deg,
                                          Tmag=args.tmag, Teff=[args.teff_min, args.teff_max], rad=[0.05, args.rad_max])
            break
        except Exception as e:
            log(f"  cone {i} attempt {attempt+1} failed: {e}"); time.sleep(5)
    else:
        continue
    df = tab[["ID", "ra", "dec", "Tmag", "Teff", "rad", "mass", "d", "contratio", "lumclass", "pmRA", "pmDEC", "Kmag"]].to_pandas()
    df["cone"] = i
    frames.append(df)
    pd.concat(frames, ignore_index=True).to_csv(partial, index=False)
    if i % 8 == 7:
        log(f"  {i+1}/{len(cones)} cones, {sum(len(f) for f in frames)} rows so far")
if not os.path.exists(pool_cache):
    pool = pd.concat(frames, ignore_index=True).drop_duplicates("ID").drop(columns=["cone"])
    pool.to_csv(pool_cache, index=False)
    if os.path.exists(partial):
        os.remove(partial)
else:
    pool = pd.read_csv(pool_cache)
    log(f"loaded cached TIC pool from {os.path.basename(pool_cache)}")
pool["ID"] = pool["ID"].astype(str)
pool = pool[pool["lumclass"].fillna("DWARF") == "DWARF"]
log(f"TIC pool (Tmag {args.tmag[0]}-{args.tmag[1]}, Teff {args.teff_min:.0f}-{args.teff_max:.0f}, R<{args.rad_max}, dwarfs): {len(pool)} unique stars")

# ---- 3. keep stars that landed on silicon in this sector ----
out = tess_stars2px_function_entry(pool["ID"].values, pool["ra"].values, pool["dec"].values, trySector=S)
ids_on, sec_on, cam_on, ccd_on, col_on, row_on = out[0], out[3], out[4], out[5], out[6], out[7]
on = pd.DataFrame({"ID": [str(x) for x in ids_on], "sector": sec_on, "camera": cam_on, "ccd": ccd_on,
                   "col": col_on, "row": row_on})
on = on[on["sector"] == S].drop_duplicates("ID")
# stay 5 px off the CCD edge like the mission does
on = on[(on["col"] > 49) & (on["col"] < 2088) & (on["row"] > 5) & (on["row"] < 2044)]
pool = pool.merge(on, on="ID").drop_duplicates("ID")
log(f"on a CCD in sector {S}: {len(pool)}")

# ---- 4. remove 2-minute targets ----
obs = Observations.query_criteria(obs_collection="TESS", sequence_number=S, dataproduct_type="timeseries",
                                  provenance_name="SPOC")
two_min = set(str(x) for x in obs["target_name"])
pool = pool[~pool["ID"].isin(two_min)]
log(f"not a 2-minute target ({len(two_min)} of those): {len(pool)}")

# ---- 5. remove claimed stars ----
tap = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
toi = pd.read_csv(io.StringIO(requests.get(tap, params={"query": "select tid, toi, tfopwg_disp from toi", "format": "csv"}, timeout=180).text))
toi_ids = set(toi["tid"].dropna().astype(int).astype(str))
pool = pool[~pool["ID"].isin(toi_ids)]
log(f"not a TOI ({len(toi_ids)} TOIs): {len(pool)}")

ctoi_ids = set()
try:
    r = requests.get("https://exofop.ipac.caltech.edu/tess/download_ctoi.php?sort=ctoi&output=pipe",
                     headers={"User-Agent": "Mozilla/5.0 (kepler-mcp)"}, timeout=120)
    if r.ok and not r.text.lstrip().startswith("<"):
        ct = pd.read_csv(io.StringIO(r.text), sep="|")
        col = [c for c in ct.columns if "TIC" in c][0]
        ctoi_ids = set(ct[col].dropna().astype(int).astype(str))
        pd.Series(sorted(ctoi_ids)).to_csv(os.path.join(DATA, "ctoi_tics.csv"), index=False, header=False)
    else:
        raise RuntimeError(f"HTTP {r.status_code}")
except Exception as e:
    cache = os.path.join(DATA, "ctoi_tics.csv")
    if os.path.exists(cache):
        ctoi_ids = set(pd.read_csv(cache, header=None)[0].astype(str))
        log(f"ExoFOP unreachable ({e}); using cached CTOI list ({len(ctoi_ids)})")
    else:
        log(f"ExoFOP unreachable ({e}) and no cache; CTOI filter SKIPPED, rerun later")
pool = pool[~pool["ID"].isin(ctoi_ids)]
log(f"not a CTOI ({len(ctoi_ids)} CTOIs): {len(pool)}")

ps = pd.read_csv(io.StringIO(requests.get(tap, params={"query": "select distinct tic_id from ps where tic_id is not null", "format": "csv"}, timeout=180).text))
known = set(ps["tic_id"].dropna().astype(str).str.replace("TIC", "").str.strip())
pool = pool[~pool["ID"].isin(known)]
log(f"not a known planet host ({len(known)} hosts): {len(pool)}")

excl = os.path.join(ROOT, "ops", "private_exclude.txt")
if os.path.exists(excl):
    private = set(open(excl).read().split())
    pool = pool[~pool["ID"].isin(private)]
    log(f"not on the private exclusion list ({len(private)} TICs): {len(pool)}")

# ---- 6. rank and save ----
# expected transit depth for a Jupiter (0.1 Rsun) in front of this star, as a quick detectability score
pool["jupiter_depth_ppm"] = (0.10 / pool["rad"]) ** 2 * 1e6
pool = pool.sort_values("Tmag").reset_index(drop=True)
path = os.path.join(DATA, f"targets_s{S}.csv")
pool.to_csv(path, index=False)
log(f"wrote {len(pool)} targets to {path}")
print(pool[["ID", "Tmag", "Teff", "rad", "camera", "ccd", "jupiter_depth_ppm"]].head(10).to_string(index=False))
