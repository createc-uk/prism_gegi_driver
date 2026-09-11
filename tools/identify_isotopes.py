#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline isotope identification from a saved GeGI N42 spectrum.

Runs the driver's screening layer (matched-filter peak search + nuclide
library match, see src/phds_gegi_driver/isotope_id.py) on a *_spectrum.n42
file written by the data recorder. IDENTIFIES what is present across the full
SNM/IND/NORM library; it does not quantify (activity = the assay layer).

Self-contained (no ROS): runs in the Windows plotting .venv or in WSL.

Examples:
  python tools/identify_isotopes.py --n42 data/20260807_165302_spectrum.n42
  python tools/identify_isotopes.py --n42 <file> --no-shield      # plate out
  python tools/identify_isotopes.py --n42 <file> --fit-fwhm       # check res model
"""
from __future__ import print_function

import argparse
import math
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'src', 'phds_gegi_driver'))
import isotope_id  # noqa: E402


def read_n42(path):
    """Parse the recorder's N42: returns (counts, channel_center_energies_keV,
    live_time_s, real_time_s). Calibration is linear: E = offset + gain*ch."""
    with open(path) as f:
        text = f.read()
    m = re.search(r'<CoefficientValues>([^<]+)</CoefficientValues>', text)
    if not m:
        raise SystemExit("No <CoefficientValues> in %s" % path)
    coeffs = [float(v) for v in m.group(1).split()]
    offset, gain = coeffs[0], coeffs[1]
    m = re.search(r'<ChannelData[^>]*>([^<]+)</ChannelData>', text)
    if not m:
        raise SystemExit("No <ChannelData> in %s" % path)
    counts = np.array([float(v) for v in m.group(1).split()], dtype=np.float64)

    def _duration(tag):
        mm = re.search(r'<%s>PT([0-9.]+)S</%s>' % (tag, tag), text)
        return float(mm.group(1)) if mm else 0.0

    energies = offset + gain * (np.arange(len(counts)) + 0.5)
    return counts, energies, _duration('LiveTimeDuration'), _duration('RealTimeDuration')


def fit_fwhm(counts, energies, peaks):
    """Measured FWHM of strong peaks (net second moment over a local-linear-
    background-subtracted window) vs the library resolution model."""
    rows = []
    for p in peaks:
        if p['snr'] < 10.0:
            continue
        e0 = p['energy_keV']
        w_model = isotope_id.fwhm_kev(e0)
        half = 3.0 * w_model
        sel = (energies > e0 - half) & (energies < e0 + half)
        if sel.sum() < 5:
            continue
        x, y = energies[sel], counts[sel]
        bg = np.interp(x, [x[0], x[-1]], [y[0], y[-1]])   # local linear bg
        net = np.clip(y - bg, 0.0, None)
        if net.sum() <= 0:
            continue
        centroid = float((x * net).sum() / net.sum())
        var = float(((x - centroid) ** 2 * net).sum() / net.sum())
        rows.append((e0, 2.3548 * math.sqrt(max(var, 0.0)), w_model))
    return rows


def main():
    ap = argparse.ArgumentParser(
        description="GeGI isotope identification (screening layer, offline)")
    ap.add_argument("--n42", required=True, help="*_spectrum.n42 from a run")
    ap.add_argument("--library", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'config',
        'nuclide_library.yaml'))
    ap.add_argument("--snr", type=float, default=4.0,
                    help="peak-search SNR threshold (default 4)")
    ap.add_argument("--min-score", type=float, default=0.5,
                    help="identification score threshold (default 0.5)")
    ap.add_argument("--no-shield", action='store_true',
                    help="plate-out geometry: no transmission weighting")
    ap.add_argument("--fit-fwhm", action='store_true',
                    help="print measured vs model FWHM of strong peaks")
    ap.add_argument("--all-scores", action='store_true',
                    help="also print nuclides with any matched line")
    args = ap.parse_args()

    counts, energies, live_s, real_s = read_n42(args.n42)
    library = isotope_id.load_nuclide_library(args.library)
    shield_t = 0.0 if args.no_shield else None

    peaks, results, unknown = isotope_id.identify(
        counts, energies, library, snr_threshold=args.snr,
        min_score=args.min_score, shield_thickness_m=shield_t)

    print("=" * 72)
    print("GeGI isotope identification  (screening layer)")
    print("=" * 72)
    print("Spectrum : %s" % args.n42)
    print("Live/real: %.1f / %.1f s   channels: %d   total counts: %.0f"
          % (live_s, real_s, len(counts), counts.sum()))
    print("Peaks found (SNR >= %.1f): %d" % (args.snr, len(peaks)))
    for p in peaks:
        print("   %8.1f keV   SNR %6.1f" % (p['energy_keV'], p['snr']))

    print("-" * 72)
    print("IDENTIFIED nuclides (representative line found, score >= %.2f):"
          % args.min_score)
    any_id = False
    for r in results:
        if not r['identified']:
            continue
        any_id = True
        lines = ", ".join("%.0f keV (SNR %.0f)" % (m['energy_keV'], m['snr'])
                          for m in r['matched_lines'])
        print("  %-8s [%s]  score %.2f   lines: %s"
              % (r['nuclide'], r['category'], r['score'], lines))
        for s in r['shared_peaks']:
            print("           AMBIGUITY: %.1f keV peak also matches %s"
                  % (s['peak_keV'], "/".join(s['also'])))
    if not any_id:
        print("  (none)")

    partial = [r for r in results
               if not r['identified'] and r['matched_lines']]
    if args.all_scores and partial:
        print("-" * 72)
        print("Partial matches (NOT identified - context only):")
        for r in partial:
            lines = ", ".join("%.0f" % m['energy_keV']
                              for m in r['matched_lines'])
            print("  %-8s [%s]  score %.2f   matched: %s keV"
                  % (r['nuclide'], r['category'], r['score'], lines))

    if unknown:
        print("-" * 72)
        print("UNIDENTIFIED peaks (in no library nuclide):")
        for p in unknown:
            print("   %8.1f keV   SNR %6.1f" % (p['energy_keV'], p['snr']))

    if args.fit_fwhm:
        print("-" * 72)
        print("Resolution check (strong peaks, measured vs model FWHM):")
        for e0, w_meas, w_model in fit_fwhm(counts, energies, peaks):
            print("   %8.1f keV   measured %.2f keV   model %.2f keV"
                  % (e0, w_meas, w_model))
        print("  (update resolution: a/b in %s if these disagree)"
              % os.path.basename(args.library))
    print("=" * 72)


if __name__ == "__main__":
    main()
