"""Look a star up in the TESS Input Catalog and list the sectors that observed it."""
import re

FIELDS = ["ID", "ra", "dec", "Tmag", "Teff", "rad", "mass", "d", "contratio", "pmRA", "pmDEC", "Kmag"]


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def _fill_mdwarf(s):
    """When the catalogue has no radius or mass, estimate them from the absolute K magnitude
    (Mann et al. 2015 radius relation, Mann et al. 2019 mass relation; valid for M dwarfs, 4.6 < M_K < 9.8)."""
    import math
    if (s.get("rad") and s.get("mass")) or not (s.get("Kmag") and s.get("d")):
        return
    mk = s["Kmag"] - 5 * math.log10(s["d"] / 10.0)
    if not 4.6 < mk < 9.8:
        return
    if not s.get("rad"):
        s["rad"] = 1.9515 - 0.3520 * mk + 0.01680 * mk ** 2
        s["rad_source"] = "estimated from absolute K magnitude (Mann et al. 2015)"
    if not s.get("mass"):
        x = mk - 7.5
        s["mass"] = 10 ** (-0.647 - 0.208 * x - 8.43e-4 * x ** 2 + 7.87e-3 * x ** 3 + 1.42e-4 * x ** 4 - 2.13e-4 * x ** 5)
        s["mass_source"] = "estimated from absolute K magnitude (Mann et al. 2019)"


def position_at(s, sector):
    """Catalogue position (epoch 2000) moved by proper motion to the middle of a TESS sector. Returns (ra, dec) in degrees."""
    import math
    pmra, pmdec = s.get("pmRA") or 0.0, s.get("pmDEC") or 0.0
    years = (2018.56 + (int(sector) - 1) * 27.4 / 365.25) - 2000.0
    dec = s["dec"] + pmdec * years / 3.6e6
    ra = s["ra"] + pmra * years / 3.6e6 / max(math.cos(math.radians(s["dec"])), 1e-6)
    return ra, dec


def resolve(query):
    """query: 'TIC 261136679', a bare TIC number, a name MAST can resolve ('Pi Mensae'),
    or 'ra dec' in decimal degrees. Returns a plain dict; raises LookupError when nothing matches."""
    from astroquery.mast import Catalogs
    q = str(query).strip()
    m = re.fullmatch(r"(?i)(?:tic[\s\-_]*)?(\d{4,12})", q)
    if m:
        t = Catalogs.query_criteria(catalog="Tic", ID=int(m.group(1)))
    else:
        t = Catalogs.query_object(q, radius=0.006, catalog="Tic")   # ~1 TESS pixel
        if len(t):
            t.sort("dstArcSec")
    if len(t) == 0:
        raise LookupError(f"no TIC entry found for {query!r}")
    # TIC keeps some stars twice: a row marked DUPLICATE/SPLIT/ARTIFACT points to the real one (duplicate_id).
    # Proxima Centauri resolved to its DUPLICATE row, and the planet catalogue knows only the main row.
    note = None
    bad = {"DUPLICATE", "SPLIT", "ARTIFACT", "PHANTOM"}
    disp = lambda row: str(row["disposition"]).strip().upper() if "disposition" in row.colnames else ""
    if disp(t[0]) in bad:
        main = str(t[0]["duplicate_id"]).strip() if "duplicate_id" in t.colnames else ""
        if main.isdigit():
            note = f"TIC {int(t[0]['ID'])} is a {disp(t[0]).lower()} catalogue entry; using the main entry TIC {main}"
            t = Catalogs.query_criteria(catalog="Tic", ID=int(main))
        elif not m:
            keep = [i for i in range(len(t)) if disp(t[i]) not in bad]
            if keep:
                note = f"skipped a {disp(t[0]).lower()} catalogue entry at this position"
                t = t[keep]
    c = t[0]
    out = {k: _num(c[k]) for k in FIELDS}
    out["ID"] = int(c["ID"])
    if not m and "dstArcSec" in c.colnames:
        out["matched_at_arcsec"] = _num(c["dstArcSec"])
    if note:
        out["catalogue_note"] = note
    _fill_mdwarf(out)
    from tesshunt.followup import observed_sectors
    out["sectors"] = observed_sectors(out["ID"], out["ra"], out["dec"])
    return out
