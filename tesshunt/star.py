"""Look a star up in the TESS Input Catalog and list the sectors that observed it."""
import re

FIELDS = ["ID", "ra", "dec", "Tmag", "Teff", "rad", "mass", "d", "contratio"]


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


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
    c = t[0]
    out = {k: _num(c[k]) for k in FIELDS}
    out["ID"] = int(c["ID"])
    if not m:
        out["matched_at_arcsec"] = _num(c["dstArcSec"])
    from tesshunt.followup import observed_sectors
    out["sectors"] = observed_sectors(out["ID"], out["ra"], out["dec"])
    return out
