#!/usr/bin/env python
"""Cs-137 : Co-60 ratio check (DJR Assay Experiment G).

Reads mixed-source assay activity CSV(s) (both Cs-137 and Co-60 present) and
reports, for each run and across the set:

  * activity_ratio  A(Cs-137)/A(Co-60)  -- the physical isotopic ratio. Depends
    on BOTH calibration factors, so it is a JOINT check of the Cs and Co
    calibration: it should match the decay-corrected certificate ratio.
  * count_ratio     net(Cs-137)/net(Co-60)  -- raw net counts, calibration
    INDEPENDENT. A stable fingerprint of the source mix (folds in the fixed
    efficiency ratio); use it as a QC metric that survives a recalibration.

Co-60 is averaged over its two photopeaks (1173 & 1332 keV), exactly as the
driver's group_by_radionuclide / cs137_co60_ratio do. The two half-lives differ
(Cs-137 30.08 y, Co-60 5.27 y) so the TRUE ratio drifts with time -- the
certificate ratio is therefore decay-corrected to the measurement date.

Self-contained (no ROS / rospy import) so it runs in the Windows plotting .venv
as well as in WSL. Python 2.7 or 3.

Examples:
  # single mixed run, compared to the two certificates
  python tools/ratio_check.py --csv data/Experiment\\ G/<ts>_activity.csv \\
      --cs-cert 2.88 --co-cert 1.13 --cert-date 2026-06-01 --tolerance 5

  # reproducibility over several mixed runs (no certificate needed)
  python tools/ratio_check.py --csvs "data/Experiment G"/*_activity.csv
"""
from __future__ import print_function

import argparse
import csv
import glob
import math
from datetime import datetime

# Half-lives in DAYS (DDEP / NNDC), matching tools/validate_efficiency.py.
HALF_LIFE_DAYS = {
    "Cs137": 30.08 * 365.25,
    "Co60":  5.2711 * 365.25,
}


def base_nuclide(label):
    """'Co60_1332' -> 'Co60', 'Cs137' -> 'Cs137'."""
    return (label or "").split("_")[0]


def decay_factor(nuclide, cert_date, meas_date):
    t_half = HALF_LIFE_DAYS[nuclide]
    dt_days = (meas_date - cert_date).total_seconds() / 86400.0
    return math.exp(-math.log(2.0) * dt_days / t_half)


def read_run(csv_path):
    """Aggregate one activity CSV into per-nuclide {activity_MBq, net_counts}.

    Cs-137 = its single line; Co-60 = mean activity of its two lines and the SUM
    of their net counts (the driver's group_by_radionuclide convention). Mirrors
    validate_efficiency: prefer valid rows, fall back to any positive row.
    """
    per_line_act = {}     # label -> list of activities (usually one row/label)
    per_line_net = {}     # label -> summed net_corrected
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            label = (row.get("isotope") or "").strip()
            if not label:
                continue
            try:
                act = float(row.get("activity_MBq", "0") or 0)
                net = float(row.get("net_corrected", "0") or 0)
            except ValueError:
                continue
            valid = str(row.get("valid", "")).strip().lower() in ("true", "1")
            per_line_act.setdefault(label, []).append((act, valid))
            per_line_net[label] = per_line_net.get(label, 0.0) + net

    # Collapse lines to nuclides.
    nuc = {}
    for base in ("Cs137", "Co60"):
        labels = [l for l in per_line_act if base_nuclide(l) == base]
        if not labels:
            continue
        line_means = []
        for l in labels:
            vals = [a for (a, v) in per_line_act[l] if v and a > 0]
            if not vals:
                vals = [a for (a, v) in per_line_act[l] if a > 0]
            if vals:
                line_means.append(sum(vals) / len(vals))
        if not line_means:
            continue
        nuc[base] = {
            "activity_MBq": sum(line_means) / len(line_means),   # avg the lines
            "net_counts": sum(per_line_net[l] for l in labels),  # sum the lines
            "lines": sorted(labels),
        }
    return nuc


def ratios(nuc):
    """(activity_ratio, count_ratio) as Cs-137/Co-60, or (None, None)."""
    cs, co = nuc.get("Cs137"), nuc.get("Co60")
    if not cs or not co:
        return None, None
    a = cs["activity_MBq"] / co["activity_MBq"] if co["activity_MBq"] > 0 else None
    c = cs["net_counts"] / co["net_counts"] if co["net_counts"] > 0 else None
    return a, c


def mean_sd(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, 0
    m = sum(vals) / len(vals)
    if len(vals) > 1:
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
    else:
        sd = 0.0
    return m, sd, len(vals)


def main():
    p = argparse.ArgumentParser(
        description="GeGI Cs-137:Co-60 ratio check (Exp G)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--csv", help="single mixed-source *_activity.csv")
    g.add_argument("--csvs", nargs="+", help="several mixed-source CSVs")
    p.add_argument("--cs-cert", type=float, default=None,
                   help="Cs-137 certificate activity (MBq)")
    p.add_argument("--co-cert", type=float, default=None,
                   help="Co-60 certificate activity (MBq)")
    p.add_argument("--cert-date", default=None,
                   help="certificate date YYYY-MM-DD (shared by both sources)")
    p.add_argument("--meas-date", default=None,
                   help="measurement date YYYY-MM-DD (default: today)")
    p.add_argument("--tolerance", type=float, default=5.0,
                   help="pass/fail band on the activity ratio, percent")
    args = p.parse_args()

    paths = [args.csv] if args.csv else list(args.csvs)
    # allow a quoted glob to be passed as one arg
    expanded = []
    for pth in paths:
        hits = glob.glob(pth)
        expanded.extend(sorted(hits) if hits else [pth])
    paths = expanded

    ref_ratio = None
    if args.cs_cert and args.co_cert and args.cert_date:
        cert_date = datetime.strptime(args.cert_date, "%Y-%m-%d")
        meas_date = (datetime.strptime(args.meas_date, "%Y-%m-%d")
                     if args.meas_date else datetime.now())
        f_cs = decay_factor("Cs137", cert_date, meas_date)
        f_co = decay_factor("Co60", cert_date, meas_date)
        a_cs, a_co = args.cs_cert * f_cs, args.co_cert * f_co
        ref_ratio = a_cs / a_co

    print("=" * 68)
    print("GeGI Cs-137 : Co-60 ratio check  (DJR Assay Experiment G)")
    print("=" * 68)
    if ref_ratio is not None:
        print("Certificate (decay-corrected to %s):" %
              meas_date.strftime("%Y-%m-%d"))
        print("  Cs-137 %.4g MBq / Co-60 %.4g MBq  ->  ref ratio = %.4f  <-- truth"
              % (a_cs, a_co, ref_ratio))
        print("-" * 68)

    a_list, c_list = [], []
    for pth in paths:
        nuc = read_run(pth)
        a, c = ratios(nuc)
        name = pth.replace("\\", "/").split("/")[-1]
        if a is None:
            print("%-40s : both nuclides not present -> skipped" % name)
            continue
        a_list.append(a)
        c_list.append(c)
        line = ("%-40s : A(Cs)/A(Co)=%.4f  net(Cs)/net(Co)=%.4f"
                % (name, a, c if c is not None else float('nan')))
        if ref_ratio is not None:
            bias = (a / ref_ratio - 1.0) * 100.0
            line += "  bias %+.1f%% %s" % (
                bias, "PASS" if abs(bias) <= args.tolerance else "FAIL")
        print(line)

    if len(a_list) > 1:
        am, asd, an = mean_sd(a_list)
        cm, csd, cn = mean_sd(c_list)
        print("-" * 68)
        print("Activity ratio : mean %.4f  SD %.4f  (RSD %.1f%%, n=%d)"
              % (am, asd, 100.0 * asd / am if am else 0.0, an))
        print("Count ratio    : mean %.4f  SD %.4f  (RSD %.1f%%, n=%d)  "
              "[calibration-independent]"
              % (cm, csd, 100.0 * csd / cm if cm else 0.0, cn))
        if ref_ratio is not None:
            print("Activity ratio bias vs certificate: %+.1f%%"
                  % ((am / ref_ratio - 1.0) * 100.0))
    print("=" * 68)
    print("Note: activity_ratio validates BOTH calibrations at once; count_ratio "
          "is a\ncalibration-free fingerprint of the source mix. Co-60 is the "
          "PRIMARY\nreference (its continuum sits under the Cs-137 662 keV peak, "
          "not vice-versa).")


if __name__ == "__main__":
    main()
