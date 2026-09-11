#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Fit the detector energy-scale correction from a multi-line calibration run.

The GeGI's onboard event-energy calibration reads slightly high at the top of
the range (~+1.2 keV at 1332 keV, ~0 below ~200 keV - revealed by the 0.2 keV
binning). This tool measures precise photopeak centroids of known reference
lines in a saved N42 (ideally a Cs-137 + Co-60 + Eu-152 run: a 10-point ladder
from 122 to 1408 keV) and fits the correction

    E_true = c0 + c1*E_meas + c2*E_meas^2

printing the coefficients (for the driver's energy-correction config) and the
per-line residuals so the fit quality is visible. A linear fit is shown for
comparison; use the quadratic unless the linear residuals are just as small.

Centroids use a NARROW window (+-1.2 FWHM) around the tallest channel near
each reference line, with local linear background subtraction - deliberately
tight so the single-site photopeak dominates and the summed-event satellite
peaks a few keV away do not pull the centroid.

Usage:
  python tools/fit_energy_cal.py --n42 data/<ts>_spectrum.n42
"""
from __future__ import print_function

import argparse
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'src', 'phds_gegi_driver'))
import isotope_id            # noqa: E402
from identify_isotopes import read_n42   # noqa: E402

# Reference lines (evaluated energies, keV). Strong, unblended choices from
# the Cs/Co/Eu calibration set. 1085.84 is excluded: it has a real Eu
# companion at 1089.74 close enough to bias a centroid.
REFERENCE_LINES = [
    (121.78, "Eu-152"),
    (244.70, "Eu-152"),
    (344.28, "Eu-152"),
    (661.66, "Cs-137"),
    (778.90, "Eu-152"),
    (964.06, "Eu-152"),
    (1112.07, "Eu-152"),
    (1173.23, "Co-60"),
    (1332.49, "Co-60"),
    (1408.01, "Eu-152"),
]


def measure_centroid(counts, energies, e_ref, search_kev=3.0, min_net=50.0):
    """Net centroid of the tallest peak within +-search_kev of e_ref.

    Window = +-1.2 FWHM(model) around the maximum channel; local linear
    background from the window edges. Returns (centroid, net_counts) or None.
    """
    sel = (energies > e_ref - search_kev) & (energies < e_ref + search_kev)
    if sel.sum() < 5:
        return None
    idx = np.where(sel)[0]
    peak_i = idx[np.argmax(counts[idx])]
    half = 1.2 * isotope_id.fwhm_kev(e_ref)
    win = (energies > energies[peak_i] - half) & \
          (energies < energies[peak_i] + half)
    if win.sum() < 3:
        return None
    x, y = energies[win], counts[win]
    bg = np.interp(x, [x[0], x[-1]], [y[0], y[-1]])
    net = np.clip(y - bg, 0.0, None)
    total = float(net.sum())
    if total < min_net:
        return None
    return float((x * net).sum() / total), total


def main():
    ap = argparse.ArgumentParser(
        description="Fit the GeGI energy-scale correction from a saved N42")
    ap.add_argument("--n42", required=True,
                    help="calibration run (Cs+Co+Eu recommended)")
    args = ap.parse_args()

    counts, energies, live_s, _ = read_n42(args.n42)
    counts = counts.copy()
    counts[-1] = 0.0     # overflow bin

    meas, true, labels, nets = [], [], [], []
    for e_ref, who in REFERENCE_LINES:
        r = measure_centroid(counts, energies, e_ref)
        if r is None:
            print("  %-8s %8.2f keV : peak too weak - skipped" % (who, e_ref))
            continue
        c, net = r
        meas.append(c)
        true.append(e_ref)
        labels.append(who)
        nets.append(net)

    if len(meas) < 4:
        raise SystemExit("Only %d usable lines - need a stronger/longer run."
                         % len(meas))
    meas = np.array(meas)
    true = np.array(true)

    # Quadratic and linear fits of E_true as a function of E_measured.
    q = np.polyfit(meas, true, 2)      # [c2, c1, c0]
    l = np.polyfit(meas, true, 1)      # [c1, c0]
    rq = true - np.polyval(q, meas)
    rl = true - np.polyval(l, meas)

    print("=" * 74)
    print("GeGI energy-scale correction fit   (%s, live %.0f s)"
          % (os.path.basename(args.n42), live_s))
    print("=" * 74)
    print("%-8s %10s %10s %8s %8s | %9s %9s"
          % ("line", "true", "measured", "offset", "net", "resid(q)", "resid(l)"))
    for i in range(len(meas)):
        print("%-8s %10.2f %10.2f %+8.2f %8.0f | %+9.3f %+9.3f"
              % (labels[i], true[i], meas[i], meas[i] - true[i], nets[i],
                 rq[i], rl[i]))
    print("-" * 74)
    print("Quadratic: E_true = %.6g + %.8g*E + %.6g*E^2   (RMS %.3f, max %.3f keV)"
          % (q[2], q[1], q[0],
             math.sqrt(float(np.mean(rq ** 2))), float(np.max(np.abs(rq)))))
    print("Linear   : E_true = %.6g + %.8g*E              (RMS %.3f, max %.3f keV)"
          % (l[1], l[0],
             math.sqrt(float(np.mean(rl ** 2))), float(np.max(np.abs(rl)))))
    print("-" * 74)
    print("Driver config (energy correction, applied per event energy):")
    print("  energy_cal_c0: %.6g" % q[2])
    print("  energy_cal_c1: %.8g" % q[1])
    print("  energy_cal_c2: %.6g" % q[0])
    print("=" * 74)


if __name__ == "__main__":
    main()
