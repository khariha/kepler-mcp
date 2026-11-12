"""Local cache of FFI pixel cubes (astrocut TPFs) so vetting scripts can rerun offline."""
import os
import lightkurve as lk
from tesshunt.sweep_core import get_tpf
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUBES = os.path.join(ROOT, "data", "cubes")

def cube_path(tic, sector, size=15):
    return os.path.join(CUBES, f"s{sector}_tic{tic}_{size}x{size}.fits")

def load_tpf(tic, sector, ra, dec, size=15):
    """Cached TPF for (tic, sector); fetches from MAST S3 and saves when missing. None if the sector has no cube."""
    p = cube_path(tic, sector, size)
    if os.path.exists(p):
        return lk.TessTargetPixelFile(p)
    tpf = get_tpf(ra, dec, sector, size, "s3")
    if tpf is None:
        return None
    os.makedirs(CUBES, exist_ok=True)
    tpf.hdu.writeto(p, overwrite=True)
    return lk.TessTargetPixelFile(p)
