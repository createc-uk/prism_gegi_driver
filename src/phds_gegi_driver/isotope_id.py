# -*- coding: utf-8 -*-
"""Isotope identification (screening) layer for the GeGI driver.

Two stages, mirroring the pipeline of Kim et al., J. Korean Phys. Soc. 87
(2025) 1144 (H3D M400 pHGI, Becquerel-style), adapted for HPGe resolution:

  1. matched_filter_snr / find_peaks - an automatic peak search over the FULL
     spectrum. A zero-sum Gaussian-curvature ("Mexican hat") kernel, scaled to
     the detector FWHM at each energy, is correlated with the counts. The
     kernel has zero response to constant and linear continuum, so a weak peak
     riding on a huge Compton continuum from a dominant nuclide still stands
     out - the failure mode of fixed-ROI sideband subtraction. The response is
     normalized by its Poisson standard deviation, giving a per-channel SNR
     that is scale-free (works at any count level).

  2. match_nuclides - found peaks are matched against a nuclide library
     (config/nuclide_library.yaml). Each nuclide is scored by the fraction of
     its DETECTABILITY-weighted line set that was found, where the weight is
     yield x shield-plate transmission at that energy. This replaces the
     paper's strict AND logic: a nuclide is not rejected because one weak (or
     plate-blinded) line is below detection. Identification requires the
     representative line plus a score threshold.

This layer IDENTIFIES ("what is present?"); it does not quantify. Activity
comes from the calibrated ROI/Currie assay layer (activity_node +
data_recorder). Pure module: no rospy - unit-testable and usable offline
(tools/identify_isotopes.py). Python 2.7 / 3.x.
"""
from __future__ import print_function

import math

import numpy as np
import yaml


# ---------------------------------------------------------------------------
# Detector / attenuation models
# ---------------------------------------------------------------------------

def fwhm_kev(energy_kev, a=0.0034, b=0.31):
    """Detector FWHM (keV) at energy E: sqrt(a*E + b). Defaults ~planar HPGe."""
    return math.sqrt(max(1e-9, a * float(energy_kev) + b))


# NIST XCOM mass attenuation coefficients for iron (cm^2/g), log-log
# interpolated. Consistent with the per-line mu values in isotopes.yaml
# (662 keV: 0.0729 * 7.87 = 57.4/m checks out).
_FE_E_KEV = [50.0, 60.0, 80.0, 100.0, 150.0, 200.0, 300.0, 400.0, 500.0,
             600.0, 800.0, 1000.0, 1250.0, 1500.0, 2000.0, 3000.0]
_FE_MU_RHO = [1.958, 1.205, 0.5952, 0.3717, 0.1964, 0.1460, 0.1099, 0.0940,
              0.08414, 0.07704, 0.06699, 0.05995, 0.05350, 0.04883, 0.04265,
              0.03621]
_FE_LOG_E = np.log(_FE_E_KEV)
_FE_LOG_MU = np.log(_FE_MU_RHO)


def steel_transmission(energy_kev, thickness_m, density_g_cm3=7.87):
    """Photon transmission through a steel plate at the given energy.

    Log-log interpolation of the iron mu/rho table; energies outside the
    table clamp to its ends. thickness_m <= 0 -> 1.0 (no plate).
    """
    t_cm = float(thickness_m) * 100.0
    if t_cm <= 0.0:
        return 1.0
    log_e = math.log(min(max(float(energy_kev), _FE_E_KEV[0]), _FE_E_KEV[-1]))
    mu_rho = math.exp(float(np.interp(log_e, _FE_LOG_E, _FE_LOG_MU)))
    return math.exp(-mu_rho * density_g_cm3 * t_cm)


# ---------------------------------------------------------------------------
# Stage 1: matched-filter peak search
# ---------------------------------------------------------------------------

def matched_filter_snr(counts, energies_kev, res_a=0.0034, res_b=0.31):
    """Per-channel peak SNR from a zero-sum Gaussian-curvature matched filter.

    counts: array of channel counts; energies_kev: channel CENTER energies
    (monotonic, ~linear binning). For each channel the kernel width follows
    the detector FWHM at that energy. Returns an SNR array (same length);
    channels too close to the spectrum edges get SNR 0.
    """
    y = np.asarray(counts, dtype=np.float64)
    e = np.asarray(energies_kev, dtype=np.float64)
    n = len(y)
    snr = np.zeros(n, dtype=np.float64)
    if n < 8:
        return snr
    bin_w = float(np.median(np.diff(e)))
    if bin_w <= 0:
        return snr

    for j in range(n):
        sigma_ch = fwhm_kev(e[j], res_a, res_b) / 2.3548 / bin_w
        if sigma_ch < 0.6:
            sigma_ch = 0.6          # never narrower than the binning allows
        half = int(math.ceil(4.0 * sigma_ch))
        lo, hi = j - half, j + half + 1
        if lo < 0 or hi > n:
            continue
        x = np.arange(-half, half + 1, dtype=np.float64) / sigma_ch
        kernel = (1.0 - x * x) * np.exp(-0.5 * x * x)
        kernel -= kernel.mean()     # exactly zero-sum: nulls flat continuum
        window = y[lo:hi]
        signal = float(np.dot(kernel, window))
        var = float(np.dot(kernel * kernel, window))
        if var > 0.0:
            snr[j] = signal / math.sqrt(var)
    return snr


def find_peaks(counts, energies_kev, res_a=0.0034, res_b=0.31,
               snr_threshold=4.0):
    """Significant peaks in the spectrum: list of dicts sorted by energy.

    A peak = a local maximum of the SNR track above threshold, isolated so
    that only the highest channel within +-1 FWHM survives. The energy is
    refined by parabolic interpolation of the SNR through the maximum.
    Returns [{'energy_keV', 'snr', 'channel'}, ...].
    """
    y = np.asarray(counts, dtype=np.float64)
    e = np.asarray(energies_kev, dtype=np.float64)
    snr = matched_filter_snr(y, e, res_a, res_b)
    n = len(snr)
    candidates = [j for j in range(1, n - 1)
                  if snr[j] >= snr_threshold
                  and snr[j] >= snr[j - 1] and snr[j] >= snr[j + 1]]
    # Suppress shoulders: keep only the strongest candidate within 1 FWHM -
    # or 2 bin widths when the binning is coarser than the resolution (else a
    # single peak spread over adjacent coarse bins splits into two).
    bin_w = float(np.median(np.diff(e)))
    candidates.sort(key=lambda j: -snr[j])
    kept = []
    for j in candidates:
        w = max(fwhm_kev(e[j], res_a, res_b), 2.0 * bin_w)
        if all(abs(e[j] - e[k]) > w for k in kept):
            kept.append(j)
    peaks = []
    for j in sorted(kept):
        # Parabolic refinement of the SNR maximum.
        s0, s1, s2 = snr[j - 1], snr[j], snr[j + 1]
        denom = (s0 - 2.0 * s1 + s2)
        shift = 0.5 * (s0 - s2) / denom if abs(denom) > 1e-12 else 0.0
        shift = max(-1.0, min(1.0, shift))
        bin_w = e[min(j + 1, n - 1)] - e[j] if j + 1 < n else 0.0
        peaks.append({'energy_keV': float(e[j] + shift * bin_w),
                      'snr': float(s1),
                      'channel': int(j)})
    return peaks


# ---------------------------------------------------------------------------
# Stage 2: library matching
# ---------------------------------------------------------------------------

def load_nuclide_library(path):
    """Load config/nuclide_library.yaml -> dict (resolution, shielding,
    nuclides). Raises on a structurally invalid file."""
    with open(path) as f:
        lib = yaml.safe_load(f) or {}
    if 'nuclides' not in lib or not lib['nuclides']:
        raise ValueError("nuclide library has no 'nuclides' section: %s" % path)
    for name, nuc in lib['nuclides'].items():
        lines = nuc.get('lines') or []
        if not lines:
            raise ValueError("nuclide %s has no lines" % name)
        rep = float(nuc.get('representative_keV', 0.0) or 0.0)
        if not any(abs(float(l['energy_keV']) - rep) < 0.5 for l in lines):
            raise ValueError(
                "nuclide %s: representative_keV %.2f not in its lines"
                % (name, rep))
    return lib


def match_nuclides(peaks, library, min_score=0.5, match_sigma=1.0,
                   shield_thickness_m=None, min_lines_alt=3,
                   alt_min_score=0.2):
    """Score every library nuclide against the found peaks.

    A line MATCHES the nearest peak within max(match_sigma*FWHM, 1.0 keV) of
    its energy. Line weight = yield x steel transmission at that energy (511
    keV annihilation lines carry zero weight - they are not nuclide-specific).
    score = matched_weight / total_weight.

    A nuclide is 'identified' when EITHER
      (a) its representative line matched AND score >= min_score (a nuclide
          entry may override the threshold with its own 'min_score', e.g.
          U-238 whose low-energy Th-234 lines are self-absorbed by the
          uranium matrix), OR
      (b) MULTI-LINE EVIDENCE: >= min_lines_alt distinct lines matched and
          score >= alt_min_score. Three independent ~1-keV energy
          coincidences are decisive regardless of which lines they are -
          this rescues a nuclide whose representative line is too weak for
          the current statistics (e.g. Eu-152 blazing at 122/245/344/779 keV
          while its 1408 keV representative is below detection).

    After scoring, a SHARED-PEAK DEMOTION pass kills single-line free-riders:
    an identified nuclide whose matched peaks are ALL shared is demoted when
    a co-claimant has >= 2 unshared lines of its own (the peak is already
    explained - e.g. Eu-152's 121.8 keV line would otherwise false-identify
    Co-57 at 122.06 keV). Symmetric single-line ambiguities (U-235 185.7 vs
    Ra-226 186.2 on one peak) demote NEITHER - both stay, flagged.

    Returns a list sorted by (identified desc, score desc) of dicts:
      {nuclide, category, score, identified, matched_lines, missed_lines,
       shared_peaks}
    where shared_peaks lists peak energies also claimed by another nuclide
    (e.g. U-235 185.7 vs Ra-226 186.2) - ambiguities to resolve by context.
    """
    res = library.get('resolution', {}) or {}
    res_a = float(res.get('a', 0.0034))
    res_b = float(res.get('b', 0.31))
    shield = library.get('shielding', {}) or {}
    if shield_thickness_m is None:
        shield_thickness_m = float(shield.get('thickness_m', 0.0) or 0.0)
    density = float(shield.get('density_g_cm3', 7.87) or 7.87)

    peak_energies = [p['energy_keV'] for p in peaks]
    claims = {}     # peak index -> [nuclide names]
    results = []
    for name in sorted(library['nuclides']):
        nuc = library['nuclides'][name]
        matched, missed = [], []
        matched_w = total_w = 0.0
        rep = float(nuc.get('representative_keV', 0.0) or 0.0)
        rep_found = False
        for line in nuc['lines']:
            e_l = float(line['energy_keV'])
            weight = float(line['yield'])
            if line.get('annihilation'):
                weight = 0.0    # 511 keV is not nuclide-specific
            else:
                weight *= steel_transmission(e_l, shield_thickness_m, density)
            total_w += weight
            tol = max(match_sigma * fwhm_kev(e_l, res_a, res_b), 1.0)
            best = None
            for i, e_p in enumerate(peak_energies):
                d = abs(e_p - e_l)
                if d <= tol and (best is None or d < best[1]):
                    best = (i, d)
            if best is not None:
                i = best[0]
                matched_w += weight
                matched.append({'energy_keV': e_l,
                                'peak_keV': peaks[i]['energy_keV'],
                                'snr': peaks[i]['snr'],
                                'weight': weight,
                                'peak_index': i})
                claims.setdefault(i, []).append(name)
                if abs(e_l - rep) < 0.5:
                    rep_found = True
            elif weight > 0.0:
                missed.append({'energy_keV': e_l, 'weight': weight})
        score = (matched_w / total_w) if total_w > 0.0 else 0.0
        threshold = float(nuc.get('min_score', min_score))
        n_real = sum(1 for m in matched if m['weight'] > 0.0)
        identified = bool(
            (rep_found and score >= threshold) or
            (n_real >= min_lines_alt and score >= alt_min_score))
        results.append({'nuclide': name,
                        'category': nuc.get('category', ''),
                        'score': score,
                        'identified': identified,
                        'matched_lines': matched,
                        'missed_lines': missed,
                        'shared_peaks': []})

    # Shared-peak demotion: an identified nuclide whose EVERY matched peak is
    # shared is a free-rider when some co-claimant already explains those
    # peaks with independent evidence (>= 2 unshared lines of its own).
    by_name = {r['nuclide']: r for r in results}
    unshared_count = {}
    for r in results:
        unshared_count[r['nuclide']] = sum(
            1 for m in r['matched_lines']
            if len(claims.get(m['peak_index'], [])) == 1)
    for r in results:
        if not r['identified'] or not r['matched_lines']:
            continue
        if unshared_count[r['nuclide']] > 0:
            continue        # has independent evidence of its own
        subsumers = set()
        for m in r['matched_lines']:
            for other in claims.get(m['peak_index'], []):
                if (other != r['nuclide']
                        and by_name[other]['identified']
                        and unshared_count[other] >= 2):
                    subsumers.add(other)
        if subsumers:
            r['identified'] = False
            r['subsumed_by'] = sorted(subsumers)

    # Ambiguity flags: peaks claimed by more than one nuclide.
    for i, names in claims.items():
        if len(names) > 1:
            e_p = peaks[i]['energy_keV']
            for r in results:
                if r['nuclide'] in names:
                    r['shared_peaks'].append(
                        {'peak_keV': e_p,
                         'also': sorted(n for n in names if n != r['nuclide'])})
    results.sort(key=lambda r: (not r['identified'], -r['score']))
    return results


def unidentified_peaks(peaks, results):
    """Peaks not matched by ANY nuclide - the 'what is that?' list."""
    matched_energies = set()
    for r in results:
        for m in r['matched_lines']:
            matched_energies.add(round(m['peak_keV'], 3))
    return [p for p in peaks
            if round(p['energy_keV'], 3) not in matched_energies]


def label_peaks(peaks, results, strong_snr=30.0, annihilation_tol_kev=3.0):
    """Per-peak display labels: the identified nuclide name(s) for matched
    peaks ('A/B' when shared), '?' for peaks in no library nuclide, plus
    physics tags:
      - 'annihilation'        : the peak sits at 511 keV (never
                                nuclide-specific on its own);
      - 'backscatter-suspect' : the peak lies in the 180-200 keV backscatter
                                region AND a strong (SNR >= strong_snr) peak
                                exists above 400 keV - a strong source's
                                backscatter bump can mimic real lines there
                                (Ra-226 186 / U-235 185.7).
    Pure function; the recorder publishes this list on /identified_lines so
    the spectrum display, heatmap and N42 all carry IDENTICAL labels.
    """
    names = {}       # peak -> identified nuclides (the label)
    candidates = {}  # peak -> matching but NOT-identified nuclides (honesty)
    for r in results:
        if r.get('identified'):
            target = names
        elif r.get('subsumed_by'):
            continue    # actively explained away - casts no doubt
        else:
            target = candidates
        for m in r.get('matched_lines', []):
            key = round(m['peak_keV'], 3)
            if key not in target:
                target[key] = []
            if r['nuclide'] not in target[key]:
                target[key].append(r['nuclide'])
    strong_high = any(p['snr'] >= strong_snr and p['energy_keV'] > 400.0
                      for p in peaks)
    out = []
    for p in peaks:
        key = round(p['energy_keV'], 3)
        tags = []
        if key in names:
            label = "/".join(names[key])
            # CONTESTED: an unidentified library nuclide also fits this peak
            # (e.g. a lone 122 keV: Co-57 122.06 vs Eu-152 121.78). The label
            # says so instead of feigning certainty.
            if key in candidates:
                label += '?'
                tags.append('also-matches:' + "/".join(candidates[key]))
        elif key in candidates:
            # Unknown, but a library nuclide would fit if more of its lines
            # were seen - a hint, not an identification.
            label = '?'
            tags.append('candidates:' + "/".join(candidates[key]))
        else:
            label = '?'
        if abs(p['energy_keV'] - 511.0) <= annihilation_tol_kev:
            tags.append('annihilation')
        if 180.0 <= p['energy_keV'] <= 200.0 and strong_high:
            tags.append('backscatter-suspect')
        out.append({'energy_keV': round(float(p['energy_keV']), 2),
                    'snr': round(float(p['snr']), 1),
                    'label': label,
                    'tags': tags})
    return out


def energy_drift_kev(results):
    """SNR-weighted mean (measured peak - library line) energy offset over the
    matched lines of identified nuclides. Returns (drift_kev, n_lines).

    A consistent non-zero drift across strong known lines means the ENERGY
    CALIBRATION is off - which silently degrades every label match and the
    assay ROIs. The recorder warns when |drift| exceeds a threshold.
    """
    num = den = 0.0
    n = 0
    for r in results:
        if not r.get('identified'):
            continue
        for m in r.get('matched_lines', []):
            w = max(float(m.get('snr', 1.0)), 1.0)
            num += w * (float(m['peak_keV']) - float(m['energy_keV']))
            den += w
            n += 1
    return (num / den, n) if den > 0.0 else (0.0, 0)


def mark_persistent(lines, prev_energies, tol_kev=1.5, snr_instant=10.0):
    """Label-flicker suppression: set 'persistent' True on lines whose energy
    also appeared in the PREVIOUS screening pass (within tol_kev). Displays
    should only draw persistent labels; a statistical wiggle that fires once
    never gets a name.

    A peak with SNR >= snr_instant is persistent IMMEDIATELY - a >=10-sigma
    peak is not a statistical wiggle, and making an obvious source wait a
    full extra screening period for its name costs response time for nothing.

    Returns this pass's energies (feed back as prev_energies next call).
    Pure function - caller owns the state."""
    for l in lines:
        l['persistent'] = (float(l.get('snr', 0.0)) >= snr_instant
                           or any(abs(l['energy_keV'] - e) <= tol_kev
                                  for e in (prev_energies or [])))
    return [l['energy_keV'] for l in lines]


def identify(counts, energies_kev, library, snr_threshold=4.0, min_score=0.5,
             shield_thickness_m=None):
    """Full pipeline: peak search + library match on one spectrum.

    Returns (peaks, results, unknown) - the found peaks, the per-nuclide
    scores (identified first), and the unmatched peaks.
    """
    res = library.get('resolution', {}) or {}
    peaks = find_peaks(counts, energies_kev,
                       res_a=float(res.get('a', 0.0034)),
                       res_b=float(res.get('b', 0.31)),
                       snr_threshold=snr_threshold)
    results = match_nuclides(peaks, library, min_score=min_score,
                             shield_thickness_m=shield_thickness_m)
    return peaks, results, unidentified_peaks(peaks, results)
