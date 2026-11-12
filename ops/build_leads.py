"""Operator tool. Turn sweep results into the leads queue file.

    python ops/build_leads.py --sectors 7 8            # reads ops/data/sweep_s<N>.csv and targets_s<N>.csv, writes ops/leads.jsonl

A lead is looser than a candidate on purpose: hunters should meet impostors (eclipsing binaries, blends) as well as survivors.
Rotating stars are dropped because there is nothing to vet. Kept: search SNR >= 8, at least 2 transits, a dip over 5 sigma,
duration within 1.5x the physical maximum plus one cadence, and a duty cycle under 15%.
TICs listed in ops/private_exclude.txt never become leads.
"""
import argparse, json, os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "ops", "data")


def lead_rows(sw, tg, min_snr=8.0, min_sigma=5.0):
    sw = sw[sw.status == "ok"].drop_duplicates("ID").copy()
    cad_h = sw.get("cadence_min", pd.Series(30.0, index=sw.index)).fillna(30.0) / 60
    ok = ((sw.snr >= min_snr) & (sw.n_transits >= 2) & (sw.depth_ppm > 0)
          & (sw.depth_ppm / sw.depth_err_ppm >= min_sigma)
          & (sw.duration_h <= 1.5 * sw.max_duration_h + cad_h + 1e-6)
          & (sw.duration_h / 24 / sw.period_d < 0.15))
    out = []
    tg = tg.drop_duplicates("ID").set_index("ID")
    for r in sw[ok].itertuples(index=False):
        t = tg.loc[r.ID] if r.ID in tg.index else None
        star = {"Tmag": r.Tmag, "Teff": r.Teff, "rad": r.rad}
        if t is not None:
            for k in ("mass", "d", "contratio"):
                if k in t and pd.notna(t[k]):
                    star[k] = float(t[k])
        star = {k: (round(float(v), 3) if v is not None and pd.notna(v) else None) for k, v in star.items()}
        out.append({"tic": int(r.ID), "sector": None, "period_d": round(float(r.period_d), 6), "t0": round(float(r.t0), 5),
                    "duration_h": round(float(r.duration_h), 2), "depth_ppm": round(float(r.depth_ppm)), "snr": round(float(r.snr), 1), "star": star})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sectors", type=int, nargs="+", required=True)
    ap.add_argument("--min-snr", type=float, default=8.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "ops", "leads.jsonl"))
    a = ap.parse_args()
    excl = os.path.join(ROOT, "ops", "private_exclude.txt")
    private = set(open(excl).read().split()) if os.path.exists(excl) else set()
    leads, seen = [], set()
    for s in a.sectors:
        sw = pd.read_csv(os.path.join(DATA, f"sweep_s{s}.csv"))
        tg = pd.read_csv(os.path.join(DATA, f"targets_s{s}.csv"))
        rows = lead_rows(sw, tg, a.min_snr)
        kept = 0
        for r in rows:
            if str(r["tic"]) in private or r["tic"] in seen:      # one lead per star: the first sector that saw it
                continue
            r["sector"] = s; seen.add(r["tic"]); leads.append(r); kept += 1
        print(f"sector {s}: {len(sw)} stars swept, {(sw.status == 'ok').mean() * 100:.0f}% ok, {len(rows)} pass the lead bar, {kept} kept")
    with open(a.out, "w") as f:
        for r in sorted(leads, key=lambda r: -r["snr"]):
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(leads)} leads to {a.out}")


if __name__ == "__main__":
    main()
