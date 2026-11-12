"""Pick the stars worth searching on one TESS detector: observed there, not already claimed by anyone.

Used by the hub to build patches. One detector (a 12-degree square, one of 16 per sector) takes a minute or two.
"""
import io, os, time
import numpy as np
import pandas as pd
import requests

TAP = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"
COLS = ["ID", "ra", "dec", "Tmag", "Teff", "rad", "mass", "d", "contratio", "lumclass", "pmRA", "pmDEC", "Kmag"]


def _cached(cache_dir, name, max_age_s, fetch):
    """Small text cache for slow catalogue pulls; falls back to a stale copy when the archive is down."""
    p = os.path.join(cache_dir, name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_s:
        return set(open(p).read().split())
    try:
        ids = set(str(x) for x in fetch())
        os.makedirs(cache_dir, exist_ok=True)
        open(p, "w").write("\n".join(sorted(ids)))
        return ids
    except Exception:
        if os.path.exists(p):
            return set(open(p).read().split())
        raise


def claimed_ids(cache_dir, sector):
    """TICs that are not ours to search: TOIs, known planet hosts, community candidates, and the sector's 2-minute targets."""
    def toi():
        return pd.read_csv(io.StringIO(requests.get(TAP, params={"query": "select tid from toi", "format": "csv"}, timeout=180).text))["tid"].dropna().astype(int)

    def hosts():
        ps = pd.read_csv(io.StringIO(requests.get(TAP, params={"query": "select distinct tic_id from ps where tic_id is not null", "format": "csv"}, timeout=180).text))
        return ps["tic_id"].dropna().astype(str).str.replace("TIC", "").str.strip()

    def ctoi():
        r = requests.get("https://exofop.ipac.caltech.edu/tess/download_ctoi.php?sort=ctoi&output=pipe", headers={"User-Agent": "Mozilla/5.0 (kepler-mcp)"}, timeout=120)
        r.raise_for_status()
        ct = pd.read_csv(io.StringIO(r.text), sep="|")
        return ct[[c for c in ct.columns if "TIC" in c][0]].dropna().astype(int)

    def two_min():
        from astroquery.mast import Observations
        obs = Observations.query_criteria(obs_collection="TESS", sequence_number=sector, dataproduct_type="timeseries", provenance_name="SPOC")
        return [str(x) for x in obs["target_name"]]

    day = 86400
    out = _cached(cache_dir, "toi.txt", day, toi) | _cached(cache_dir, "hosts.txt", day, hosts) | _cached(cache_dir, f"twomin_s{sector}.txt", 30 * day, two_min)
    try:
        out |= _cached(cache_dir, "ctoi.txt", day, ctoi)
    except Exception:
        pass      # ExoFOP is often down; the hub re-checks the catalogue for every lead anyway
    return out


def select_detector(sector, camera, ccd, cache_dir, exclude=frozenset(), tmag=(12.0, 15.0), teff=(2300.0, 4000.0), rad_max=0.6, cone_deg=5.5, log=lambda m: None):
    """Stars on one detector of one sector inside the magnitude, temperature and radius limits (defaults: faint red dwarfs),
    minus everything already claimed and everything in `exclude`. Returns a list of plain dicts."""
    from astropy.coordinates import SkyCoord, SkyOffsetFrame
    import astropy.units as u
    from astroquery.mast import Catalogs
    from tess_stars2px import tess_stars2px_function_entry, TESS_Spacecraft_Pointing_Data
    sc = TESS_Spacecraft_Pointing_Data()
    idx = list(sc.sectors).index(sector)
    center = SkyCoord(ra=float(sc.camRa[camera - 1][idx]) * u.deg, dec=float(sc.camDec[camera - 1][idx]) * u.deg)
    frame = SkyOffsetFrame(origin=center)
    pts = [SkyCoord(lon=dx * u.deg, lat=dy * u.deg, frame=frame).transform_to("icrs") for dx in (-9, -3, 3, 9) for dy in (-9, -3, 3, 9)]
    o = tess_stars2px_function_entry(np.arange(len(pts)), np.array([p.ra.deg for p in pts]), np.array([p.dec.deg for p in pts]), trySector=sector)
    mine = sorted({int(i) for i, s, cam, cc in zip(o[0], o[3], o[4], o[5]) if s == sector and cam == camera and cc == ccd})
    if not mine:
        return []
    frames = []
    for i in mine:
        for attempt in range(3):
            try:
                tab = Catalogs.query_criteria(catalog="Tic", coordinates=f"{pts[i].ra.deg} {pts[i].dec.deg}", radius=cone_deg,
                                              Tmag=list(tmag), Teff=list(teff), rad=[0.05, rad_max])
                frames.append(tab[COLS].to_pandas()); break
            except Exception as e:
                log(f"cone {i} attempt {attempt + 1} failed: {e}"); time.sleep(4)
        else:
            raise RuntimeError("the TESS Input Catalog could not be reached; try again in a few minutes")
        log(f"cone {mine.index(i) + 1} of {len(mine)}: {sum(len(f) for f in frames)} stars so far")
    pool = pd.concat(frames, ignore_index=True).drop_duplicates("ID")
    pool = pool[(pool["lumclass"].fillna("DWARF") == "DWARF") & (pool["contratio"].fillna(0) <= 1.0)]   # skip stars outshone by their neighbours
    pool["ID"] = pool["ID"].astype(str)
    o = tess_stars2px_function_entry(pool["ID"].values.astype(np.int64), pool["ra"].values.astype(float), pool["dec"].values.astype(float), trySector=sector)
    on = pd.DataFrame({"ID": [str(x) for x in o[0]], "sector": o[3], "camera": o[4], "ccd": o[5], "col": o[6], "row": o[7]})
    on = on[(on.sector == sector) & (on.camera == camera) & (on.ccd == ccd) & (on.col > 49) & (on.col < 2088) & (on.row > 5) & (on.row < 2044)].drop_duplicates("ID")
    pool = pool.merge(on[["ID"]], on="ID").drop_duplicates("ID")
    n_on = len(pool)
    taken = claimed_ids(cache_dir, sector) | set(str(x) for x in exclude)
    pool = pool[~pool["ID"].isin(taken)].sort_values("Tmag")
    log(f"sector {sector} camera {camera} detector {ccd}: {n_on} stars on silicon, {len(pool)} unclaimed")
    f = lambda v: None if pd.isna(v) else round(float(v), 6)
    return [{"tic": int(r.ID), "ra": f(r.ra), "dec": f(r.dec), "Tmag": f(r.Tmag), "Teff": f(r.Teff), "rad": f(r.rad), "mass": f(r.mass),
             "d": f(r.d), "contratio": f(r.contratio), "pmRA": f(r.pmRA), "pmDEC": f(r.pmDEC), "Kmag": f(r.Kmag)} for r in pool.itertuples(index=False)]
