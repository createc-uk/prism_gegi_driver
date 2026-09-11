#!/usr/bin/env python
"""
Data Recorder Node - Saves acquisition data at the end of a timed acquisition.

Wraps the detector's start_timed_acquisition command with a proxy that:
  1. Starts newline-delimited-JSON (.jsonl) recording of Compton events
  2. Accumulates spectrum data for N42 export
  3. Collects heatmap peak directions and isotope IDs for CSV export
  4. Collects activity results for CSV export
  5. When the timer expires, stops recording and writes all files

Output files (in --output-dir, default /opt/phds_gegi_driver/data):
  - <timestamp>_compton_events.jsonl
  - <timestamp>_spectrum.n42
  - <timestamp>_heatmap.csv
  - <timestamp>_activity.csv

CLI options:
  --output-dir (str): Directory for saved files (default: /opt/phds_gegi_driver/data)

Command channel (see prism_command_channel.py):
  gegi.<node-name>.command / command_result, commands:
    "start_timed_recording" {"duration_minutes": N} - starts timed
        acquisition AND recording.
    "stop_recording" - stops recording early and saves data collected so far.
    "clear_all" - clears the detector hardware plus every downstream node's
        accumulator buffer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import csv
import json
import logging
import math
import struct
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import prism
import yaml

import prism_messages as pmsg
from prism_command_channel import CommandServer, CommandClient

# Isotope-ID screening layer (matched-filter peak search + nuclide library
# match). Optional: the recorder runs without it if the module or a
# --nuclide-library path is not configured.
try:
    import isotope_id
except ImportError:
    isotope_id = None

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("data_recorder_node")

# Fixed topic names (not CLI-overridable; see prism_messages.py for schemas).
COMPTON_EVENT_TOPIC = "gegi.driver.compton_event"
SPECTRUM_TOPIC = "gegi.spectrum.histogram"
SOURCE_DIRECTIONS_TOPIC = "gegi.heatmap.source_directions"
SOURCE_ISOTOPES_TOPIC = "gegi.heatmap.source_isotopes"
ACTIVITY_RESULTS_TOPIC = "gegi.activity.results"
EFFECTIVE_SOURCE_DISTANCE_TOPIC = "gegi.activity.effective_source_distance"
CLOUD_TOPIC = "gegi.heatmap.cloud"
CLOUD_META_TOPIC = "gegi.heatmap.cloud_meta"

DETECTOR_COMMAND_TOPIC = "gegi.detector.command"
DETECTOR_COMMAND_RESULT_TOPIC = "gegi.detector.command_result"
DETECTOR_RUN_INFO_TOPIC = "gegi.detector.run_info"

# Isotope-ID screening results: compact pipe-separated "Name:score" text (raw,
# no JSON wrapper - same convention as gegi.heatmap.source_isotopes), e.g.
# "Eu-152:0.83|Cs-137:1.00", or "none". Published by this node's periodic
# screening pass over the rolling live spectrum; consumed by
# spherical_heatmap_node (parse_identified_msg + identified_bands) to widen
# its imaging bands to nuclides beyond the default Cs-137/Co-60, and by
# tools/plot_live_spectrum.py to label peaks.
IDENTIFIED_TOPIC = "gegi.data_recorder.identified"

DEFAULT_NODE_CLEAR_TARGETS = (
    "gegi.spherical_heatmap.command:clear,"
    "gegi.spectrum.command:clear,"
    "gegi.spectrum_singles.command:clear,"
    "gegi.activity.command:clear"
)


# Specific gamma-ray dose-rate constants (uSv*m^2 / MBq*h). The authoritative
# values live in config/isotopes.yaml under `gamma_constants`; this dict is only
# the fallback used when that file is missing or lacks the section.
DEFAULT_GAMMA_CONSTANTS = {'Cs-137': 0.0771, 'Co-60': 0.3059}


def _norm_iso(name):
    """Normalise an isotope name for lookup: uppercase, drop punctuation.
    So 'Cs137', 'Cs-137', 'cs_137' and 'Co60_1173' all collapse sensibly."""
    return ''.join(ch for ch in str(name).upper() if ch.isalnum())


def load_gamma_constants(config_path):
    """Load specific gamma-ray dose-rate constants from isotopes.yaml.

    Returns a dict keyed by the normalised isotope name so lookups work whether
    the caller passes the yaml form ('Cs137') or the display form ('Cs-137').
    Falls back to DEFAULT_GAMMA_CONSTANTS if the file is missing/unreadable so
    dose weighting never hard-fails on a config problem.
    """
    table = {_norm_iso(k): float(v) for k, v in DEFAULT_GAMMA_CONSTANTS.items()}
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            for k, v in (cfg.get('gamma_constants', {}) or {}).items():
                table[_norm_iso(k)] = float(v)
        except Exception as exc:  # noqa: broad - never let dose weighting crash
            logger.warning("Could not load gamma_constants from %s: %s",
                            config_path, exc)
    return table


def gamma_constant_for(table, iso_name, default=0.077):
    """Look up a gamma constant tolerant of naming ('Co-60' vs 'Co60_1173')."""
    key = _norm_iso(iso_name)
    if key in table:
        return table[key]
    # Fall back to a prefix match (e.g. 'CO601173' -> 'CO60').
    for k, v in table.items():
        if key.startswith(k) or k.startswith(key):
            return v
    return default


def screening_xml_block(id_results, unknown_peaks, exclude_names=()):
    """N42 <Nuclide> entries for SCREENING identifications + an unknown-peaks
    remark. Pure function (unit-tested).

    Screening = the isotope-ID layer's spectral library match: it says a
    nuclide IS PRESENT but carries no activity (no calibration for it). Any
    nuclide already reported by the assay layer (quantified or MDA) is
    excluded so it is not listed twice. Returns (list_of_nuclide_xml, remark).
    """
    blocks = []
    for r in id_results or []:
        if not r.get('identified') or r.get('nuclide') in exclude_names:
            continue
        lines_txt = ", ".join(
            "{:.0f} keV (SNR {:.0f})".format(m['energy_keV'], m['snr'])
            for m in r.get('matched_lines', []))
        shared = "".join(
            " AMBIGUITY: {:.1f} keV peak also matches {}.".format(
                s['peak_keV'], "/".join(s['also']))
            for s in r.get('shared_peaks', []))
        blocks.append(
            '      <Nuclide>\n'
            '        <NuclideIdentifiedIndicator>true</NuclideIdentifiedIndicator>\n'
            '        <NuclideName>{name}</NuclideName>\n'
            '        <Remark>SCREENING identification (spectral library match; '
            'no calibration, so no activity is quoted): category {cat}, '
            'score {score:.2f}, lines {lines}.{shared}</Remark>\n'
            '      </Nuclide>'.format(
                name=r['nuclide'], cat=r.get('category', ''),
                score=r.get('score', 0.0), lines=lines_txt, shared=shared))
    remark = ''
    if unknown_peaks:
        remark = ('    <Remark>Screening: unidentified peaks at {} - matching '
                  'no library nuclide.</Remark>\n'.format(
                      ", ".join("{:.1f} keV (SNR {:.0f})".format(
                          p['energy_keV'], p['snr']) for p in unknown_peaks)))
    return blocks, remark


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else 0.5 * (ordered[mid - 1] + ordered[mid])


def dead_time_correction_factor(real_time_s, live_time_s, dead_time_percent=None):
    """Multiplier that converts a RAW net area/activity to a DEAD-TIME-CORRECTED one.

    The detector's live-time clock already excludes dead time, so real/live is
    the exact correction and is preferred. If the real/live counters are missing
    or nonsensical we fall back to the reported cumulative dead-time percentage,
    and finally to 1.0 (no correction) so a bad run-info read can never fabricate
    or destroy counts.

    IMPORTANT (calibration coupling): the calibration_factor values in
    isotopes.yaml were fitted against certificated sources using UNCORRECTED
    activities, so they already absorb the ~2% dead time of the commissioning
    runs. Turning this correction on therefore REQUIRES re-deriving the
    calibration factors from dead-time-corrected commissioning data, otherwise
    the ~2% is applied twice -- see docs/PORTING.md. This is why
    apply_dead_time_correction defaults to OFF in this driver (unlike the
    upstream ROS1 fork, which defaults it ON): that recalibration has not been
    done here yet.
    """
    try:
        rt = float(real_time_s)
        lt = float(live_time_s)
        if rt > 0.0 and lt > 0.0 and lt <= rt:
            return rt / lt
    except (TypeError, ValueError):
        pass
    try:
        dt = float(dead_time_percent)
        if 0.0 <= dt < 100.0:
            return 1.0 / (1.0 - dt / 100.0)
    except (TypeError, ValueError):
        pass
    return 1.0


def currie_critical_level(background_counts):
    """Currie critical level L_C = 2.33*sqrt(B): the net-count DECISION threshold
    (~95% confidence) above which a line is DETECTED. Background-aware, unlike a
    fixed count gate."""
    return 2.33 * math.sqrt(max(0.0, float(background_counts or 0.0)))


def currie_mda_bq(total_background_counts, efficiency_product, total_live_time_s):
    """Currie detection limit L_D converted to activity (Bq).

    L_D = 2.71 + 4.65*sqrt(B) NET counts (95% confidence, Currie 1968), then
    A = L_D / (K * t_live) with K the efficiency product reported per line by the
    activity node (A_Bq = net / (K * t_live)). Returns None when it cannot be
    computed (no efficiency/geometry, or no live time).
    """
    try:
        K = float(efficiency_product)
        t = float(total_live_time_s)
    except (TypeError, ValueError):
        return None
    if K <= 0.0 or t <= 0.0:
        return None
    B = max(0.0, float(total_background_counts or 0.0))
    L_D = 2.71 + 4.65 * math.sqrt(B)
    return L_D / (K * t)


def aggregate_run_activity(activity_reports, drop_threshold=0.75, dt_factor=1.0,
                           background_rates=None):
    """Collapse the per-window activity reports into ONE result per gamma line,
    deciding DETECTION on the RUN-TOTAL net.

    Detection is a run-level Currie decision: a line is detected when its total
    net over the whole run exceeds L_C = 2.33*sqrt(B_total), NOT when individual
    counting windows each clear a fixed count threshold. A weak line (e.g. Co-60's
    1332 keV) that never crosses the per-window bar but accumulates ample counts
    over the run is therefore still quantified. The activity node's per-window
    ``valid`` flag is deliberately IGNORED here - it remains only a live-display
    hint.

    Activity is TOTAL net / TOTAL live time (correctly window-weighted), scaled by
    the per-line efficiency product K reported by the activity node
    (A_Bq = total_net/(K*total_live)); older/synthetic reports without K fall back
    to the window activity/net scale.

    Partial start-up windows are still EXCLUDED, on COUNT RATE: a ramp window has
    partial counts against a full logged live time, so its rate is anomalously low
    (including it dragged a real run -6.8%). A genuinely short window has a NORMAL
    rate and is kept.

    background_rates (optional) = {label: {net_cps, net_cps_sigma}} from a no-source
    run: its net counts (rate x live) are subtracted per line, and its variance is
    folded into the detection threshold AND the counting sigma. This removes both
    the environmental peaks (e.g. ambient Cs-137) and the zero-background false
    positives (an empty ROI no longer has L_C = 0).

    Returns {line_label: {...}} for DETECTED lines only; undetected configured
    lines are reported as MDAs by non_detected_nuclide_mdas().
    """
    detection_floor_counts = 5.0
    per_line = {}
    for report in activity_reports:
        live = float(report.get('live_time_s', 0.0) or 0.0)
        for iso in report.get('isotopes', []):
            name = iso.get('isotope', '')
            net = float(iso.get('net_corrected', 0.0) or 0.0)
            if not name or net <= 0 or live <= 0:
                continue
            per_line.setdefault(name, []).append({
                'net': net, 'live': live,
                'gross': float(iso.get('gross_counts', 0.0) or 0.0),
                'bg': float(iso.get('background_counts', 0.0) or 0.0),
                'K': float(iso.get('efficiency_product', 0.0) or 0.0),
                'act': float(iso.get('activity_MBq', 0.0) or 0.0),
                'energy_keV': float(iso.get('energy_keV', 0.0) or 0.0),
                # Position-aware correction provenance (activity node folds the
                # factor into K, so the activity is already corrected; these
                # only document it in the N42).
                'pos_f': float(iso.get('position_factor', 1.0) or 1.0),
                'slant': float(iso.get('slant_distance_m', 0.0) or 0.0),
            })

    results = {}
    for name, windows in per_line.items():
        # Reference rate from SUBSTANTIVE windows only: a seconds-long flush
        # fragment carries huge rate variance and, with only two windows, can
        # drag the median above the genuine full window's rate - which then
        # gets dropped as "partial" while the fragment is kept. Fragments are
        # still rate-filtered and summed normally; they just cannot steer the
        # reference.
        max_live = max(w['live'] for w in windows)
        substantive = [w for w in windows if w['live'] >= 0.1 * max_live]
        median_rate = _median([w['net'] / w['live']
                               for w in (substantive or windows)])
        kept = [w for w in windows
                if median_rate <= 0
                or (w['net'] / w['live']) >= drop_threshold * median_rate]
        dropped = len(windows) - len(kept)
        if not kept:
            continue
        total_net = sum(w['net'] for w in kept)
        total_live = sum(w['live'] for w in kept)
        total_bg = sum(w['bg'] for w in kept)
        total_gross = sum(w['gross'] for w in kept)
        if total_net <= 0 or total_live <= 0:
            continue

        # Optional background subtraction (measured no-source rate).
        bg_counts = 0.0     # expected background counts in this run's ROI
        bg_var = 0.0        # variance of that from the background measurement
        if background_rates and name in background_rates:
            br = background_rates[name]
            bg_counts = float(br.get('net_cps', 0.0) or 0.0) * total_live
            bg_var = (float(br.get('net_cps_sigma', 0.0) or 0.0) * total_live) ** 2
        net_source = total_net - bg_counts

        # Run-level detection decision (Currie critical level). The subtracted
        # background and its measurement variance widen L_C, so a clean ROI
        # (which used to give L_C = 0) and ambient peaks no longer false-positive.
        # detection_floor_counts guards the residual B~0 pathology: with a truly
        # empty ROI L_C -> 0 and a couple of stray counts in one short partial
        # window would otherwise "detect". A handful-of-counts floor is orders
        # below any genuine assay signal.
        if net_source <= max(currie_critical_level(total_bg + bg_counts + bg_var),
                             detection_floor_counts):
            continue
        # Activity scale: prefer the reported efficiency product K (activity =
        # net_source/(K*total_live)); fall back to the window activity/net ratio for
        # reports predating it. dt_factor (real/live) scales the activity only -
        # the Poisson statistics below use RAW counts.
        ks = [w['K'] for w in kept if w['K'] > 0]
        if ks:
            activity = (net_source / (_median(ks) * total_live)) * dt_factor / 1.0e6
        else:
            ratios = [w['act'] / (w['net'] / w['live'])
                      for w in kept if w['act'] > 0 and w['net'] > 0]
            bq_per_cps = _median(ratios) if ratios else 0.0
            activity = bq_per_cps * (net_source / total_live) * dt_factor
        # Net-area sigma: Poisson on the raw counts (gross + sideband bg =
        # net + 2*bg) plus the background-subtraction variance.
        sigma_net = math.sqrt(total_net + 2.0 * total_bg + bg_var)
        # Position-corrected windows (factor meaningfully < 1) -> document the
        # median factor/slant distance in the N42. Uncorrected runs report 1.0.
        pos_windows = [w for w in kept if w.get('pos_f', 1.0) < 0.999]
        results[name] = {
            'isotope': name,
            'energy_keV': kept[0]['energy_keV'],
            'activity_MBq': activity,
            # Counting statistics ONLY - not the assay uncertainty.
            'counting_sigma_MBq': (activity * sigma_net / net_source
                                   if net_source > 0 else 0.0),
            'net_counts': net_source,
            'gross_counts': total_gross,
            'background_counts': total_bg + bg_counts,
            'live_time_s': total_live,
            'windows_used': len(kept),
            'windows_dropped': dropped,
            'position_corrected': bool(pos_windows),
            'position_factor': (_median([w['pos_f'] for w in pos_windows])
                                if pos_windows else 1.0),
            'slant_distance_m': (_median([w['slant'] for w in pos_windows])
                                 if pos_windows else 0.0),
        }
    return results


def group_by_radionuclide(line_results, radionuclide_of):
    """Combine per-line results into one result per radionuclide.

    Co-60 is measured on two photopeaks (1173 and 1332 keV) that quantify the
    SAME nuclide, so they are averaged into a single Co-60 activity. Their
    spread is reported: it is a direct internal-consistency check.
    """
    groups = {}
    for name, res in line_results.items():
        groups.setdefault(radionuclide_of(name), []).append(res)

    out = {}
    for nuclide, results in groups.items():
        activities = [r['activity_MBq'] for r in results]
        activity = sum(activities) / len(activities)
        sigma = math.sqrt(sum(r['counting_sigma_MBq'] ** 2 for r in results)) / len(results)
        spread = 0.0
        if len(activities) > 1 and activity > 0:
            spread = 100.0 * (max(activities) - min(activities)) / activity
        out[nuclide] = {
            'radionuclide': nuclide,
            'activity_MBq': activity,
            'counting_sigma_MBq': sigma,
            'line_spread_percent': spread,
            'lines': sorted(r['isotope'] for r in results),
            'net_counts': sum(r['net_counts'] for r in results),
        }
    return out


def cs137_co60_ratio(nuclides):
    """Cs-137 : Co-60 ratio for a mixed field, or None unless BOTH are detected.

    Reports two ratios (both as Cs-137 / Co-60):
      - activity_ratio: the physical isotopic ratio (calibration-corrected).
      - count_ratio: raw net counts, a calibration-INDEPENDENT fingerprint -
        useful while the calibration factors are provisional (pre-recalibration).

    Co-60 is the intended PRIMARY reference and Cs-137 the SECONDARY: Co-60's
    Compton continuum sits under the Cs-137 662 keV peak (raising its background
    and MDA) but Cs-137 does not reciprocally interfere with the Co-60 peaks.
    """
    cs = nuclides.get('Cs-137')
    co = nuclides.get('Co-60')
    if not cs or not co:
        return None
    a_cs = float(cs.get('activity_MBq', 0.0) or 0.0)
    a_co = float(co.get('activity_MBq', 0.0) or 0.0)
    net_cs = float(cs.get('net_counts', 0.0) or 0.0)
    net_co = float(co.get('net_counts', 0.0) or 0.0)
    return {
        'activity_ratio': (a_cs / a_co) if a_co > 0 else None,
        'count_ratio': (net_cs / net_co) if net_co > 0 else None,
        'cs137_MBq': a_cs,
        'co60_MBq': a_co,
    }


def run_line_totals(activity_reports):
    """Per-line RUN totals (all windows) for EVERY configured line, detected or
    not: {label: {net, gross, bg, live, energy}}. Used to build a background
    reference (which needs the undetected lines too)."""
    out = {}
    for report in activity_reports:
        live = float(report.get('live_time_s', 0.0) or 0.0)
        if live <= 0:
            continue
        for iso in report.get('isotopes', []):
            name = iso.get('isotope', '')
            if not name:
                continue
            t = out.setdefault(name, {'net': 0.0, 'gross': 0.0, 'bg': 0.0,
                                      'live': 0.0, 'energy': 0.0})
            t['net'] += float(iso.get('net_corrected', 0.0) or 0.0)
            t['gross'] += float(iso.get('gross_counts', 0.0) or 0.0)
            t['bg'] += float(iso.get('background_counts', 0.0) or 0.0)
            t['live'] += live
            t['energy'] = float(iso.get('energy_keV', 0.0) or 0.0)
    return out


def build_background_rates(activity_reports):
    """Per-line background NET RATE (cps) + 1-sigma, from a no-source run, for
    later subtraction. sigma is the Poisson net-area uncertainty per second:
    sqrt(gross + sideband_bg) / live. Returns {label: {net_cps, net_cps_sigma}}."""
    rates = {}
    for name, t in run_line_totals(activity_reports).items():
        live = t['live']
        if live <= 0:
            continue
        sigma_counts = math.sqrt(max(0.0, t['gross'] + t['bg']))
        rates[name] = {
            'net_cps': t['net'] / live,
            'net_cps_sigma': sigma_counts / live,
            'live_time_s': live,
        }
    return rates


def load_background(path):
    """Load a background reference written by build_background_rates/save.
    Returns {label: {net_cps, net_cps_sigma}} or None if absent/unreadable."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception:
        return None
    lines = data.get('lines') if isinstance(data, dict) else None
    return lines or None


def detected_line_labels(activity_reports):
    """Line labels DETECTED over the run (run-total net exceeds the Currie
    critical level). Delegates to aggregate_run_activity so the CSV filter, the
    N42 activities and the MDA decision all share ONE run-level detection rule
    rather than the activity node's per-window ``valid`` flag.
    """
    return set(aggregate_run_activity(activity_reports).keys())


def detected_nuclides(activity_reports, radionuclide_of):
    """Radionuclides detected in the run (>=1 valid window on ANY of their lines).

    Used to filter the CSV at the NUCLIDE level: if Co-60 is seen on 1173 keV,
    its 1332 keV line is kept too even when that line stayed below the per-window
    validity threshold - the two Co-60 lines are a consistency pair and dropping
    one hides that the weaker line is under threshold.
    """
    return set(radionuclide_of(l) for l in detected_line_labels(activity_reports))


def non_detected_nuclide_mdas(activity_reports, detected, radionuclide_of):
    """MDA (Bq) for each configured nuclide that was NOT detected in the run.

    A nuclide is a non-detection only if NONE of its lines were detected. Its MDA
    is bounded by its MOST SENSITIVE line (lowest MDA). Background and live time
    are summed over the whole run: more counting time -> lower (better) MDA.
    """
    total_live = 0.0
    bg = {}       # label -> summed background counts
    K = {}        # label -> efficiency product (constant per run)
    nuclide_of = {}
    for report in activity_reports:
        total_live += float(report.get('live_time_s', 0.0) or 0.0)
        for iso in report.get('isotopes', []):
            label = iso.get('isotope') or ''
            if not label:
                continue
            bg[label] = bg.get(label, 0.0) + float(iso.get('background_counts', 0.0) or 0.0)
            kp = float(iso.get('efficiency_product', 0.0) or 0.0)
            if kp > 0.0:
                K[label] = kp
            nuclide_of[label] = radionuclide_of(label)

    lines_by_nuclide = {}
    for label, nuc in nuclide_of.items():
        lines_by_nuclide.setdefault(nuc, []).append(label)

    out = {}
    for nuc, labels in lines_by_nuclide.items():
        if any(l in detected for l in labels):
            continue  # nuclide was detected on at least one line
        candidates = []
        for l in labels:
            mda = currie_mda_bq(bg.get(l, 0.0), K.get(l, 0.0), total_live)
            if mda is not None:
                candidates.append((mda, l))
        if not candidates:
            continue
        best_mda, best_line = min(candidates)
        out[nuc] = {'mda_Bq': best_mda, 'line': best_line,
                    'lines': sorted(labels), 'live_time_s': total_live}
    return out


class DataRecorderNode(object):
    def __init__(self, app, args, connection):
        self._app = app

        self.output_dir = args.get_string("output-dir")
        cal_path = args.get_string("calibration-file")
        self.calibration_file = cal_path
        self.isotopes_config = args.get_string("isotopes-config")
        self.source_distance_m = args.get_float("source-distance-m")
        self.heatmap_grid_res = int(args.get_int("heatmap-grid-res"))
        # NOTE: these two are currently unused (kept for CLI/config parity with
        # the pre-migration ~params of the same name; the settle/poll logic
        # they used to gate was removed when run-info became a continuously
        # published topic instead of an on-demand service - see _on_run_info).
        self.run_info_settle_s = args.get_float("run-info-settle-s")
        self.run_info_poll_s = args.get_float("run-info-poll-s")
        # Prefix for the durable measurement identifier written into every asset
        # so the database can link the N42, activity peak rows and heatmaps of a
        # run back to one parent Measurement (spec DB-GEGI-002 / DB-GEGI-004).
        self.measurement_id_prefix = args.get_string("measurement-id-prefix")

        # Systematic (non-counting) assay uncertainty, 1 sigma, in percent. This is
        # the position-dominated part of the uncertainty budget and is a property of
        # the METHOD, not of an individual run (see docs/DJR_assay_results_wording.md).
        #
        # The N42 activity uncertainty is built per run as
        #     u_combined = sqrt(u_counting^2 + u_systematic^2),  U(k=2) = 2*u_combined
        # so a well-counted run reports ~the budget value, while a marginal run near
        # the MDA correctly reports a much wider uncertainty. Writing the counting
        # sigma alone (~1-3%) into a durable record would badly understate the real
        # measurement uncertainty; a reader reasonably assumes the quoted figure IS
        # the measurement uncertainty.
        #
        # It is a parameter, not a constant, because the budget is rig-specific.
        #
        # Default 10.6% = RSS of: position 10.2 (Exp E), repeatability 2.1 (Exp D),
        # certificate 1.5 (Co-60 BH-4103: 3% expanded at k=2 -> 1.5% standard),
        # efficiency transfer 1.1 (Exp B), shielding 1.0 (Exp F), dead-time 0.6
        # (Exp C), Co-60 coincidence summing 0.05. -> expanded U(k=2) ~= 21.3%.
        self.assay_systematic_uncertainty_pct = args.get_float(
            "assay-systematic-uncertainty-percent")

        # Apply detector dead-time correction (real/live) to the run-aggregated
        # net areas and activities used for N42 detection/MDA (see
        # dead_time_correction_factor()). OFF by default here: the
        # calibration_factor values in isotopes.yaml were fitted against
        # UNCORRECTED activities, so turning this on without first re-deriving
        # them from dead-time-corrected commissioning data would double-count
        # the ~2% dead time.
        self.apply_dead_time_correction = bool(
            args.get_bool("apply-dead-time-correction"))

        # Report configured-but-not-detected nuclides in the N42 as a
        # non-detection with a Currie MDA ("Cs-137 not detected, < X MBq").
        # A non-detection is evidence only if it carries a limit; without this
        # the N42 simply omits absent nuclides. Enabled by default (opt out
        # with --no-report-non-detected-mda).
        self.report_non_detected_mda = not bool(
            args.get_bool("no-report-non-detected-mda"))

        # Background subtraction. Record a no-source run once
        # (--record-background) into background_file; every later run then
        # subtracts those per-line net rates, removing ambient peaks
        # (environmental Cs-137) and zero-background false positives from the
        # Currie detection decision.
        bg_file = args.get_string("background-file")
        self.background_file = bg_file or os.path.join(self.output_dir, "background.yaml")
        self.background_rates = load_background(self.background_file)
        if self.background_rates:
            logger.info("Data recorder: background subtraction ON (%d lines from %s)",
                        len(self.background_rates), self.background_file)
        self.record_background = bool(args.get_bool("record-background"))

        node_name = args.get_string("node-name")
        node_clear_targets = args.get_string("node-clear-targets")

        # Ensure output dir exists
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        # Load energy calibration for N42 export
        self.bin_edges = self._load_energy_cal(cal_path)

        # Dose-rate gamma constants, single-sourced from isotopes.yaml.
        self.gamma_constants = load_gamma_constants(self.isotopes_config)

        # Isotope-ID screening layer: an optional continuous matched-filter
        # scan of a rolling live-spectrum window, independent of the recorder's
        # recording state, plus a final full-resolution pass over the whole
        # run's accumulated spectrum at stop time (embedded in the N42).
        nuclide_library_path = args.get_string("nuclide-library")
        self.isotope_id_period_s = args.get_float("isotope-id-period-s")
        self.isotope_id_window_s = args.get_float("isotope-id-window-s")
        self.nuclide_library = None
        if nuclide_library_path and isotope_id is not None:
            try:
                self.nuclide_library = isotope_id.load_nuclide_library(nuclide_library_path)
            except Exception as exc:
                logger.warning("Could not load nuclide library %s: %s",
                               nuclide_library_path, exc)
        # Rolling window of (timestamp, spectrum_array, real_time_ms) tuples,
        # summed and screened every isotope_id_period_s regardless of whether
        # a timed recording is in progress.
        self._live_spectra = deque()
        self._live_lock = threading.Lock()
        self._prev_line_energies = []
        # Nuclides already reported (quantified or MDA) by the assay layer;
        # excluded from the screening block so they are not listed twice.
        self._screening_exclude_names = set(self._RADIONUCLIDE_MAP.values())
        self._id_stop_event = threading.Event()
        self._id_thread = None

        # Recording state
        self.lock = threading.Lock()
        self.recording = False
        self._events_file = None
        self.accumulated_spectrum = np.zeros(len(self.bin_edges), dtype=np.uint32)
        self.total_real_time_ms = 0
        self.total_live_time_ms = 0
        self.heatmap_peaks = []  # list of (x, y, z, isotope, activity)
        self.heatmap_cloud = None  # latest full point cloud (x, y, z, intensity, cs137, co60)
        self._latest_cloud_meta = None
        self.activity_results = []  # list of activity JSON dicts
        self.recording_start = None
        self.duration_minutes = 0
        self.timer = None
        self.measurement_id = ""
        self.reference_datetime = ""
        # Latest valid detector run-info, continuously updated from the
        # detector's periodically-published gegi.detector.run_info topic.
        self._last_run_info = self._empty_run_info()
        self._run_info_lock = threading.Lock()

        # -- Detector command client (replaces ServiceProxy calls to the C++
        # driver's start_timed_acquisition/stop_acquisition/clear_data services)
        det_cmd_send_cfg = prism.TextSenderConfig()
        det_cmd_send_cfg.destination = DETECTOR_COMMAND_TOPIC
        det_cmd_sender = app.create_text_sender(args, connection, det_cmd_send_cfg)

        det_cmd_recv_cfg = prism.TextReceiverConfig()
        det_cmd_recv_cfg.source = DETECTOR_COMMAND_RESULT_TOPIC
        det_cmd_receiver = app.create_text_receiver(args, connection, det_cmd_recv_cfg)

        self._detector_client = CommandClient(det_cmd_sender, det_cmd_receiver)

        # -- One-shot "clear everything" coordinator. gegi.detector.command's
        # clear_data only clears the hardware; the other nodes keep independent
        # accumulators (the heatmap holds a rolling ~120 s event buffer), so a
        # hardware clear alone leaves the live image populated until those
        # buffers age out. clear_all fans out to the hardware clear plus every
        # node clear so a single command resets the whole pipeline.
        self._node_clear_clients = self._build_node_clear_clients(
            app, args, connection, node_clear_targets)

        # -- Command channel (replaces the old ~start_timed_recording /
        # ~stop_recording / ~clear_all services) --------------------------
        command_topic = "gegi.{}.command".format(node_name)
        command_result_topic = "gegi.{}.command_result".format(node_name)

        command_recv_cfg = prism.TextReceiverConfig()
        command_recv_cfg.source = command_topic
        command_receiver = app.create_text_receiver(args, connection, command_recv_cfg)

        command_result_cfg = prism.TextSenderConfig()
        command_result_cfg.destination = command_result_topic
        command_result_sender = app.create_text_sender(args, connection, command_result_cfg)

        self.command_server = CommandServer(command_receiver, command_result_sender)
        self.command_server.on("start_timed_recording", self._handle_start)
        self.command_server.on("stop_recording", self._handle_stop)
        self.command_server.on("clear_all", self._handle_clear_all)
        self.command_server.start()

        # -- Subscribers (always active, but only record when self.recording) --
        compton_cfg = prism.TextReceiverConfig()
        compton_cfg.source = COMPTON_EVENT_TOPIC
        self.sub_compton = app.create_text_receiver(args, connection, compton_cfg)
        self.sub_compton.on_receive(self._on_compton)
        self.sub_compton.start()

        spectrum_cfg = prism.TextReceiverConfig()
        spectrum_cfg.source = SPECTRUM_TOPIC
        self.sub_spectrum = app.create_text_receiver(args, connection, spectrum_cfg)
        self.sub_spectrum.on_receive(self._on_spectrum)
        self.sub_spectrum.start()

        peaks_cfg = prism.TextReceiverConfig()
        peaks_cfg.source = SOURCE_DIRECTIONS_TOPIC
        self.sub_peaks = app.create_text_receiver(args, connection, peaks_cfg)
        self.sub_peaks.on_receive(self._on_peaks)
        self.sub_peaks.start()

        isotopes_cfg = prism.TextReceiverConfig()
        isotopes_cfg.source = SOURCE_ISOTOPES_TOPIC
        self.sub_isotopes = app.create_text_receiver(args, connection, isotopes_cfg)
        self.sub_isotopes.on_receive(self._on_isotopes)
        self.sub_isotopes.start()

        activity_cfg = prism.TextReceiverConfig()
        activity_cfg.source = ACTIVITY_RESULTS_TOPIC
        self.sub_activity = app.create_text_receiver(args, connection, activity_cfg)
        self.sub_activity.on_receive(self._on_activity)
        self.sub_activity.start()

        cloud_meta_cfg = prism.TextReceiverConfig()
        cloud_meta_cfg.source = CLOUD_META_TOPIC
        self.sub_cloud_meta = app.create_text_receiver(args, connection, cloud_meta_cfg)
        self.sub_cloud_meta.on_receive(self._on_cloud_meta)
        self.sub_cloud_meta.start()

        cloud_bin_cfg = prism.BinaryReceiverConfig()
        cloud_bin_cfg.source = CLOUD_TOPIC
        self.sub_cloud = app.create_binary_receiver(args, connection, cloud_bin_cfg)
        self.sub_cloud.on_receive(self._on_cloud)
        self.sub_cloud.start()

        # Track the effective source distance (base standoff + shielding plates)
        # published by the activity node, so saved dose/imaging use the same
        # geometry as the activity estimate.
        distance_cfg = prism.TextReceiverConfig()
        distance_cfg.source = EFFECTIVE_SOURCE_DISTANCE_TOPIC
        self.sub_distance = app.create_text_receiver(args, connection, distance_cfg)
        self.sub_distance.on_receive(self._on_source_distance)
        self.sub_distance.start()

        # Continuous run-info subscription (replaces the old one-shot
        # rospy.wait_for_service/get_run_info query). The C++ driver now
        # publishes RunInfo periodically instead of on-demand, so we just keep
        # the latest valid value cached and read it at stop time.
        run_info_cfg = prism.TextReceiverConfig()
        run_info_cfg.source = DETECTOR_RUN_INFO_TOPIC
        self.sub_run_info = app.create_text_receiver(args, connection, run_info_cfg)
        self.sub_run_info.on_receive(self._on_run_info)
        self.sub_run_info.start()

        # Latest isotope text for pairing with peaks (kept for schema-compat /
        # as a fallback if a source_directions point lacks its own 'isotope'
        # field; the primary path now reads isotope directly off each point).
        self._latest_isotope_text = ""
        # Latest activity per isotope (name -> MBq)
        self._latest_activity_MBq = {}

        # -- Isotope-ID screening results publisher --------------------------
        identified_cfg = prism.TextSenderConfig()
        identified_cfg.destination = IDENTIFIED_TOPIC
        self.pub_identified = app.create_text_sender(args, connection, identified_cfg)

        if self.nuclide_library is not None:
            self._id_thread = threading.Thread(target=self._isotope_id_loop, daemon=True)
            self._id_thread.start()

        logger.info("Data recorder node ready. Output dir: %s", self.output_dir)

    @staticmethod
    def _build_node_clear_clients(app, args, connection, targets_str):
        """Parse --node-clear-targets ("topic:command,topic:command,...")
        into one CommandClient per target, reusing the <topic>.command /
        <topic>.command_result convention used everywhere else."""
        clients = []
        for entry in targets_str.split(","):
            entry = entry.strip()
            if not entry:
                continue
            topic, _, cmd = entry.partition(":")
            topic = topic.strip()
            cmd = cmd.strip() or "clear"
            if topic.endswith(".command"):
                result_topic = topic[:-len(".command")] + ".command_result"
            else:
                result_topic = topic + "_result"

            send_cfg = prism.TextSenderConfig()
            send_cfg.destination = topic
            sender = app.create_text_sender(args, connection, send_cfg)

            recv_cfg = prism.TextReceiverConfig()
            recv_cfg.source = result_topic
            receiver = app.create_text_receiver(args, connection, recv_cfg)

            clients.append((CommandClient(sender, receiver), cmd, topic))
        return clients

    def stop(self):
        self._id_stop_event.set()
        if self._id_thread is not None:
            self._id_thread.join(timeout=2.0)
        self.command_server.stop()
        self.sub_compton.stop()
        self.sub_spectrum.stop()
        self.sub_peaks.stop()
        self.sub_isotopes.stop()
        self.sub_activity.stop()
        self.sub_cloud_meta.stop()
        self.sub_cloud.stop()
        self.sub_distance.stop()
        self.sub_run_info.stop()
        self._detector_client.close()
        for client, _cmd, _topic in self._node_clear_clients:
            client.close()

    def _load_energy_cal(self, path):
        if not path or not os.path.exists(path):
            return np.linspace(0.0, 3000.0, 1024)
        values = []
        with open(path, 'r') as f:
            for line in f:
                text = line.strip()
                if text:
                    values.append(float(text))
        return np.array(values, dtype=np.float64)

    def _handle_start(self, params):
        """Proxy: start timed acquisition on detector + begin recording."""
        duration_minutes = params.get("duration_minutes", 0)

        with self.lock:
            if self.recording:
                return False, "Already recording. Wait for current acquisition to finish."

        # Forward to the real detector command channel
        try:
            det_result = self._detector_client.call(
                {"command": "start_timed_acquisition", "duration_minutes": duration_minutes},
                timeout=5.0)
            if det_result is None:
                return False, "Detector command timed out"
            if not det_result.get("success"):
                return False, "Detector refused: " + det_result.get("message", "")
        except Exception as e:
            return False, "Failed to publish detector command: " + str(e)

        # Start recording
        self._start_recording(duration_minutes)

        return True, "Recording started for {} minutes. Files will be saved to {}".format(
            duration_minutes, self.output_dir)

    def _handle_stop(self, params):
        """Stop recording early and save all data collected so far."""
        with self.lock:
            if not self.recording:
                return False, "Not currently recording."
        # Cancel the timer
        if self.timer:
            self.timer.cancel()
        # Also stop the detector acquisition
        try:
            result = self._detector_client.call({"command": "stop_acquisition"}, timeout=5.0)
            if result is None or not result.get("success"):
                logger.warning("Could not stop detector acquisition (may already be stopped)")
        except Exception:
            logger.warning("Could not stop detector acquisition (may already be stopped)")
        # Save data
        self._stop_recording()
        return True, "Recording stopped early. Data saved."

    def _handle_clear_all(self, params):
        """Clear the detector hardware AND every downstream node's accumulator buffer.

        Prefers the deep onboard clear (clear_data_and_windows, GeGi 'x'
        command) over the data-only clear_data ('c'): the data-only clear was
        found to leave residual spectral content that survives into the next
        run (stale-spectrum phantoms, e.g. a persistent low-level Cs-137 line
        bleeding into a Co-only run - ported from upstream's
        handleClearDataAndWindows fix). Falls back to clear_data if the deep
        clear command is not recognised (older driver build).
        """
        results = []
        all_ok = True

        # 1) Hardware data buffer (deep clear, with a data-only fallback).
        cleared = False
        for command in ("clear_data_and_windows", "clear_data"):
            try:
                r = self._detector_client.call({"command": command}, timeout=3.0)
                ok = bool(r) and bool(r.get("success"))
                results.append("{}:{}:{}".format(
                    DETECTOR_COMMAND_TOPIC, command, "ok" if ok else "fail"))
                cleared = ok
                if ok:
                    break
            except Exception as e:
                results.append("{}:{}:err({})".format(DETECTOR_COMMAND_TOPIC, command, e))
        all_ok = cleared

        # 2) Downstream node buffers (heatmap event window, spectra, activity window)
        for client, cmd, topic in self._node_clear_clients:
            try:
                r = client.call({"command": cmd}, timeout=3.0)
                ok = bool(r) and bool(r.get("success"))
                results.append("{}:{}".format(topic, "ok" if ok else "fail"))
                all_ok = all_ok and ok
            except Exception as e:
                results.append("{}:err({})".format(topic, e))
                all_ok = False

        message = "; ".join(results)
        logger.info("clear_all: %s", message)
        return all_ok, message

    def _start_recording(self, duration_minutes):
        start_dt = datetime.now()
        timestamp = start_dt.strftime("%Y%m%d_%H%M%S")
        events_path = os.path.join(self.output_dir, "{}_compton_events.jsonl".format(timestamp))

        # Do not carry detector timing from a previous run into this recording.
        # The continuously-published RunInfo callback will populate the cache
        # again as soon as the driver publishes a valid sample for this run.
        with self._run_info_lock:
            self._last_run_info = self._empty_run_info()

        with self.lock:
            self.recording = True
            self.duration_minutes = duration_minutes
            self.recording_start = pmsg.now_seconds()
            self.accumulated_spectrum[:] = 0
            self.total_real_time_ms = 0
            self.total_live_time_ms = 0
            self.heatmap_peaks = []
            self.heatmap_cloud = None
            self.activity_results = []
            self._timestamp_prefix = timestamp
            # Durable measurement identity, stamped into every asset of this run.
            self.measurement_id = "{}-{}".format(self.measurement_id_prefix, timestamp)
            # ISO-8601 acquisition reference time (spec DB-ACT-003: activity
            # without a reference date/time is not durable).
            self.reference_datetime = start_dt.isoformat()
            self._events_file = open(events_path, 'w')

        logger.info("Recording started: %d min, events_file=%s", duration_minutes, events_path)

        # Set a timer to stop recording after the duration
        duration_secs = duration_minutes * 60.0
        self.timer = threading.Timer(duration_secs, self._stop_recording)
        self.timer.daemon = True
        self.timer.start()

    def _stop_recording(self):
        """Called when the timed acquisition ends. Save all files."""
        logger.info("Timed acquisition complete. Saving data files...")

        # Keep recording flag on briefly to capture any final heatmap/activity publishes
        # The heatmap node publishes every ~2s; wait one cycle to get final state.
        time.sleep(3.0)

        # Stop recording and snapshot totals NOW, BEFORE the run-info settle/fetch.
        # Otherwise time spent settling and reading detector run-info would keep
        # accumulating into total_real_time_ms/total_live_time_ms and inflate the
        # reported run duration (e.g. 5 min -> ~338 s).
        with self.lock:
            self.recording = False
            events_file = self._events_file
            self._events_file = None
            spectrum = self.accumulated_spectrum.copy()
            real_time_ms = self.total_real_time_ms
            live_time_ms = self.total_live_time_ms
            peaks = list(self.heatmap_peaks)
            cloud = self.heatmap_cloud
            activities = list(self.activity_results)
            prefix = self._timestamp_prefix
            measurement_id = self.measurement_id
            reference_datetime = self.reference_datetime
            duration_minutes = self.duration_minutes

        # Get detector-reported run info from the continuously-updated cache
        # (the detector publishes RunInfo periodically; see _on_run_info).
        detector_run_info = self._fetch_detector_run_info_post_stop()

        dt_factor = 1.0
        if self.apply_dead_time_correction:
            dt_factor = dead_time_correction_factor(
                detector_run_info.get('real_time_sec'),
                detector_run_info.get('live_time_sec'),
                detector_run_info.get('dead_time_percent'))
        logger.info("  Dead-time correction factor: %.5f (%s)", dt_factor,
                   "applied" if self.apply_dead_time_correction else "disabled")

        if self.record_background:
            self._write_background(activities, measurement_id)
        background_rates = None if self.record_background else self.background_rates

        # Final full-resolution isotope-ID screening pass over the WHOLE run's
        # accumulated spectrum (much better statistics than any one live-window
        # slice), embedded into the N42 as SCREENING <Nuclide> entries.
        id_results, unknown_peaks = [], []
        if self.nuclide_library is not None:
            try:
                _peaks, id_results, unknown_peaks = isotope_id.identify(
                    spectrum.astype(np.float64), self.bin_edges, self.nuclide_library)
            except Exception as exc:
                logger.warning("End-of-run isotope-ID screening failed: %s", exc)

        # Close events file
        if events_file:
            events_file.close()
            logger.info("  Events saved: %s_compton_events.jsonl", prefix)

        # Save spectrum as N42, with the run's activity results embedded so the
        # N42 is a complete standards-compliant record (spectrum + activities).
        self._save_n42(prefix, spectrum, real_time_ms, live_time_ms,
                       measurement_id, reference_datetime, activities,
                       id_results, unknown_peaks, dt_factor, background_rates)

        # Save heatmap raw points CSV (irregular, per-isotope scores)
        try:
            self._save_heatmap_csv(prefix, cloud)
        except Exception as e:
            logger.error("Failed to save heatmap raw CSV: %s", e)

        # Save heatmap rasterised CSV (regular 5mm Y-Z grid)
        try:
            self._save_heatmap_raster(prefix, cloud, peaks)
        except Exception as e:
            logger.error("Failed to save heatmap raster CSV: %s", e)

        # Save 3D heatmap grid (Y-Z projection with Gaussian blobs at peaks)
        try:
            self._save_heatmap_3d(prefix, cloud, peaks)
        except Exception as e:
            logger.error("Failed to save heatmap 3D CSV: %s", e)

        # Save activity results as CSV
        try:
            self._save_activity_csv(prefix, activities, detector_run_info,
                                    real_time_ms, live_time_ms,
                                    measurement_id, reference_datetime)
        except Exception as e:
            logger.error("Failed to save activity CSV: %s", e)

        # Save the run manifest that links every asset to one parent Measurement
        # (spec DB-GEGI-002 / DB-GEGI-004).
        try:
            self._save_manifest(prefix, measurement_id, reference_datetime,
                                duration_minutes, real_time_ms, live_time_ms,
                                detector_run_info, activities)
        except Exception as e:
            logger.error("Failed to save run manifest: %s", e)

        logger.info("All data files saved with prefix: %s (measurement_id=%s)",
                    prefix, measurement_id)

    @staticmethod
    def _empty_run_info():
        return {'valid': False, 'dead_time_percent': 0.0, 'real_time_sec': 0.0,
                'live_time_sec': 0.0, 'message': 'not_queried'}

    def _on_run_info(self, message, source=None):
        """Callback for the detector's periodically-published RunInfo state.

        Replaces the old one-shot rospy.wait_for_service/get_run_info query:
        the detector now publishes RunInfo continuously, so we simply keep
        the latest valid sample cached under a lock.
        """
        try:
            payload = json.loads(message)
            info = pmsg.parse_run_info(payload)
        except Exception:
            return
        if not info.get('valid'):
            return
        normalized = {
            'valid': True,
            'dead_time_percent': float(info.get('dead_time_percent', 0.0) or 0.0),
            'real_time_sec': float(info.get('real_time_sec', 0.0) or 0.0),
            'live_time_sec': float(info.get('live_time_sec', 0.0) or 0.0),
            'message': info.get('message', ''),
        }
        with self._run_info_lock:
            self._last_run_info = normalized

    def _fetch_detector_run_info_post_stop(self):
        """Return the run-info captured by the continuous run-info subscription.

        We do not perform a blocking query here: the detector often resets to
        idle on stop (which would read as zeros), so the last valid sample
        received while acquiring is what we want.
        """
        with self._run_info_lock:
            stored = dict(self._last_run_info)
        if stored.get('valid') and stored.get('real_time_sec', 0.0) > 0.0:
            logger.info("  Detector run info: real=%.3fs live=%.3fs dead=%.3f%%",
                        stored['real_time_sec'], stored['live_time_sec'],
                        stored['dead_time_percent'])
            return stored

        logger.warning("  Detector run info unavailable for this run")
        return self._empty_run_info()

    def _on_source_distance(self, message, source=None):
        """Track the plate-derived source distance for dose/imaging outputs."""
        try:
            payload = json.loads(message)
        except Exception:
            return
        d = pmsg.parse_double_value(payload)
        if d is None:
            return
        d = float(d)
        if d > 0 and abs(d - self.source_distance_m) > 1e-4:
            self.source_distance_m = d
            logger.info("Data recorder: source distance -> %.3fm", self.source_distance_m)

    def _on_compton(self, message, source=None):
        with self.lock:
            if not self.recording or self._events_file is None:
                return
            try:
                self._events_file.write(message + "\n")
            except Exception:
                pass

    def _on_spectrum(self, message, source=None):
        try:
            payload = json.loads(message)
            spec = pmsg.parse_spectrum(payload)
        except Exception:
            return
        arr = np.array(spec.get('spectrum', []), dtype=np.uint32)

        # Continuous isotope-ID screening window: kept regardless of whether
        # a timed recording is in progress, so a quiet screening scan is
        # always available (e.g. for the live plot tool), independent of the
        # per-run recorder state below.
        if self.nuclide_library is not None:
            with self._live_lock:
                self._live_spectra.append(
                    (time.time(), arr.astype(np.float64), spec.get('real_time_ms', 0)))
                self._trim_live_spectra()

        with self.lock:
            if not self.recording:
                return
            n = min(len(arr), len(self.accumulated_spectrum))
            self.accumulated_spectrum[:n] += arr[:n]
            self.total_real_time_ms += spec.get('real_time_ms', 0)
            self.total_live_time_ms += (spec.get('real_time_ms', 0) - spec.get('dead_time_ms', 0))

    def _trim_live_spectra(self, now=None):
        """Drop live-window samples older than isotope_id_window_s. Caller
        must hold self._live_lock."""
        now = time.time() if now is None else now
        cutoff = now - self.isotope_id_window_s
        while self._live_spectra and self._live_spectra[0][0] < cutoff:
            self._live_spectra.popleft()

    def _isotope_id_loop(self):
        """Background thread: periodically screen the rolling live-spectrum
        window and publish a compact identified-lines summary.

        Runs independent of self.recording so downstream consumers
        (spherical_heatmap_node, tools/plot_live_spectrum.py) always have a
        fresh screening result, not just during timed acquisitions.
        """
        while not self._id_stop_event.wait(self.isotope_id_period_s):
            try:
                self._run_live_screening_pass()
            except Exception as exc:  # noqa: broad - never let this thread die
                logger.warning("Isotope-ID screening pass failed: %s", exc)

    def _run_live_screening_pass(self):
        with self._live_lock:
            self._trim_live_spectra()
            samples = list(self._live_spectra)
        if not samples:
            return
        total = np.zeros(len(self.bin_edges), dtype=np.float64)
        for _t, arr, _rt in samples:
            n = min(len(arr), len(total))
            total[:n] += arr[:n]

        peaks, results, unknown = isotope_id.identify(total, self.bin_edges, self.nuclide_library)
        lines = [r for r in results if r.get('identified')]

        # Flicker suppression: a peak only counts as 'persistent' (safe to
        # display/report) once it has appeared in back-to-back passes, unless
        # it is already an obvious strong (>=10 sigma) peak.
        self._prev_line_energies = isotope_id.mark_persistent(peaks, self._prev_line_energies)

        drift_kev, n_drift_lines = isotope_id.energy_drift_kev(results)
        if n_drift_lines >= 3 and abs(drift_kev) > 3.0:
            logger.warning("Isotope-ID screening: energy calibration drift %.2f keV "
                           "over %d matched lines - check EnergyCal/energy_cal_c0..c2",
                           drift_kev, n_drift_lines)

        if lines:
            text = "|".join("{}:{:.2f}".format(r['nuclide'], r.get('score', 0.0))
                            for r in lines)
        else:
            text = "none"
        self.pub_identified.send(text)

    def _on_peaks(self, message, source=None):
        try:
            payload = json.loads(message)
        except Exception:
            return
        with self.lock:
            if not self.recording:
                return
            iso_text = self._latest_isotope_text
            # Fallback isotope labels (pipe-separated "Cs-137:count|Co-60:count"),
            # used only if a point is missing its own 'isotope' field.
            fallback_names = []
            if iso_text and iso_text != 'none':
                for part in iso_text.split('|'):
                    part = part.strip()
                    if ':' in part:
                        name = part.rsplit(':', 1)[0].strip()
                    else:
                        name = part
                    fallback_names.append(name)

            points = payload.get('points', [])
            for i, point in enumerate(points):
                iso_name = point.get('isotope') or (
                    fallback_names[i] if i < len(fallback_names) else "unknown")
                activity = self._latest_activity_MBq.get(iso_name, 0.0)
                self.heatmap_peaks.append(
                    (point.get('x', 0.0), point.get('y', 0.0), point.get('z', 0.0),
                     iso_name, activity))

    def _on_cloud_meta(self, message, source=None):
        try:
            meta = json.loads(message)
        except Exception:
            return
        with self.lock:
            self._latest_cloud_meta = meta

    def _on_cloud(self, data, source=None):
        """Store the latest sphere heatmap point cloud (overwrite each update)."""
        with self.lock:
            if not self.recording:
                return
            meta = self._latest_cloud_meta
            if meta is None:
                logger.warning("Received cloud binary before any cloud_meta; skipping")
                return

            fields = meta.get('fields', [])
            point_step = meta.get('point_step', 28)
            field_offsets = {name: idx * 4 for idx, name in enumerate(fields)}
            x_off = field_offsets.get('x', 0)
            y_off = field_offsets.get('y', 4)
            z_off = field_offsets.get('z', 8)
            intensity_off = field_offsets.get('intensity', None)
            cs137_off = field_offsets.get('cs137', None)
            co60_off = field_offsets.get('co60', None)

            raw = data
            n_points = len(raw) // point_step if point_step > 0 else 0

            # Columns: x, y, z, intensity, cs137, co60
            points = np.zeros((n_points, 6), dtype=np.float64)
            for idx in range(n_points):
                offset = idx * point_step
                x = struct.unpack_from('<f', raw, offset + x_off)[0]
                y = struct.unpack_from('<f', raw, offset + y_off)[0]
                z = struct.unpack_from('<f', raw, offset + z_off)[0]
                intensity = struct.unpack_from('<f', raw, offset + intensity_off)[0] if intensity_off is not None else 0.0
                cs137 = struct.unpack_from('<f', raw, offset + cs137_off)[0] if cs137_off is not None else 0.0
                co60 = struct.unpack_from('<f', raw, offset + co60_off)[0] if co60_off is not None else 0.0
                points[idx] = [x, y, z, intensity, cs137, co60]

            self.heatmap_cloud = points

    def _on_isotopes(self, message, source=None):
        with self.lock:
            self._latest_isotope_text = message

    def _on_activity(self, message, source=None):
        with self.lock:
            if not self.recording:
                return
            try:
                data = json.loads(message)
                self.activity_results.append(data)
                # Update latest activity per isotope for heatmap tagging
                for iso in data.get('isotopes', []):
                    name = iso.get('isotope', '')
                    # Map internal names to display names
                    if 'Cs137' in name:
                        display = 'Cs-137'
                    elif 'Co60' in name:
                        display = 'Co-60'
                    else:
                        display = name
                    mbq = iso.get('activity_MBq', 0.0)
                    if mbq > 0:
                        # For Co-60 take the max of its two peaks
                        if display in self._latest_activity_MBq:
                            self._latest_activity_MBq[display] = max(
                                self._latest_activity_MBq[display], mbq)
                        else:
                            self._latest_activity_MBq[display] = mbq
            except (ValueError, TypeError):
                pass

    def _build_analysis_results_xml(self, activities, measurement_id,
                                    id_results=None, unknown_peaks=None,
                                    dt_factor=1.0, background_rates=None):
        """Build the N42 <AnalysisResults> block: the run-level nuclide activities,
        plus (if a nuclide library is configured) SCREENING identifications for
        nuclides the isotope-ID layer sees but the assay layer does not quantify.

        N42.42 has native elements for nuclide activity results, so the spectrum
        and the activities derived from it travel together in one standards
        compliant record instead of being split across an N42 + a bespoke CSV.

        Activities are aggregated over the run (total net counts / total live
        time, partial start-up windows excluded) and reported per RADIONUCLIDE:
        Co-60's two photopeaks quantify the same nuclide and are averaged.
        Detection is a run-level Currie decision (aggregate_run_activity); a
        configured nuclide with no detected line is reported once as a bounded
        non-detection (Currie MDA) when report_non_detected_mda is enabled.
        """
        lines = aggregate_run_activity(activities, dt_factor=dt_factor,
                                       background_rates=background_rates)
        screening_blocks, screening_remark = screening_xml_block(
            id_results, unknown_peaks, exclude_names=self._screening_exclude_names)
        if not lines and not screening_blocks and not screening_remark:
            return ""
        nuclides = group_by_radionuclide(lines, self._controlled_radionuclide) if lines else {}
        u_sys = self.assay_systematic_uncertainty_pct

        nuclide_xml = []
        for name in sorted(nuclides):
            res = nuclides[name]
            activity_bq = res['activity_MBq'] * 1.0e6
            u_count = (100.0 * res['counting_sigma_MBq'] / res['activity_MBq']
                       if res['activity_MBq'] > 0 else 0.0)
            u_expanded = 2.0 * math.sqrt(u_count ** 2 + u_sys ** 2)   # k=2
            nuclide_xml.append(
                '      <Nuclide>\n'
                '        <NuclideIdentifiedIndicator>true</NuclideIdentifiedIndicator>\n'
                '        <NuclideName>{name}</NuclideName>\n'
                '        <NuclideActivityValue units="Bq">{act:.6g}</NuclideActivityValue>\n'
                '        <NuclideActivityUncertaintyValue>{unc:.6g}</NuclideActivityUncertaintyValue>\n'
                '        <Remark>Lines: {lines}. Counting u={uc:.2f}% (1sigma); '
                'systematic u={us:.2f}% (1sigma); expanded U={ue:.1f}% (k=2). '
                'Line spread {spread:.2f}%.</Remark>\n'
                '      </Nuclide>'.format(
                    name=name, act=activity_bq,
                    unc=activity_bq * u_expanded / 100.0,
                    lines=" ".join(res['lines']), uc=u_count, us=u_sys,
                    ue=u_expanded, spread=res['line_spread_percent']))

        # Non-detections: a configured nuclide with no detected line is reported
        # ONCE as a bounded non-detection (Currie MDA), which is real evidence -
        # "screened down to < X and absent" - unlike a silent omission.
        if self.report_non_detected_mda:
            detected = set(lines.keys())
            mdas = non_detected_nuclide_mdas(
                activities, detected, self._controlled_radionuclide)
            for name in sorted(mdas):
                m = mdas[name]
                mda_bq = m['mda_Bq'] * dt_factor
                nuclide_xml.append(
                    '      <Nuclide>\n'
                    '        <NuclideIdentifiedIndicator>false</NuclideIdentifiedIndicator>\n'
                    '        <NuclideName>{name}</NuclideName>\n'
                    '        <NuclideActivityValue units="Bq">0</NuclideActivityValue>\n'
                    '        <Remark>Not detected. Currie MDA (L_D, 95% confidence) '
                    '= {mbq:.6g} MBq ({bq:.6g} Bq) over live time {live:.0f} s, '
                    'bounded by line {line}. The MDA reflects the background under '
                    'the ROI during this run.</Remark>\n'
                    '      </Nuclide>'.format(
                        name=name, mbq=mda_bq / 1.0e6, bq=mda_bq,
                        live=m['live_time_s'], line=m['line']))

        peak_xml = []
        for label in sorted(lines):
            res = lines[label]
            cps = (res['net_counts'] / res['live_time_s']
                   if res['live_time_s'] > 0 else 0.0)
            pos_note = ''
            if res.get('position_corrected'):
                pos_note = (' Position-corrected from imaged hotspot: slant '
                            'distance {slant:.3f} m, efficiency factor '
                            '{pf:.4f}.'.format(slant=res['slant_distance_m'],
                                               pf=res['position_factor']))
            peak_xml.append(
                '      <Peak>\n'
                '        <PeakEnergyValue units="keV">{e:.2f}</PeakEnergyValue>\n'
                '        <PeakNetAreaValue>{net:.1f}</PeakNetAreaValue>\n'
                '        <PeakNetCountRateValue units="cps">{cps:.4f}</PeakNetCountRateValue>\n'
                '        <Remark>{label}: {used} window(s) used, {dropped} partial '
                'window(s) excluded; live time {live:.1f} s.{pos}</Remark>\n'
                '      </Peak>'.format(
                    e=res['energy_keV'], net=res['net_counts'], cps=cps,
                    label=label, used=res['windows_used'],
                    dropped=res['windows_dropped'], live=res['live_time_s'],
                    pos=pos_note))

        nuclide_xml.extend(screening_blocks)

        if not nuclide_xml and not peak_xml:
            return ""

        ref = (' radMeasurementReferences="{}"'.format(measurement_id)
               if measurement_id else '')

        # Mixed-field Cs-137 : Co-60 ratio (only when BOTH are detected).
        ratio_remark = ''
        ratio = cs137_co60_ratio(nuclides)
        if ratio is not None:
            parts = []
            if ratio['activity_ratio'] is not None:
                parts.append('activity ratio {:.3g}'.format(ratio['activity_ratio']))
            if ratio['count_ratio'] is not None:
                parts.append('raw net-count ratio {:.3g} (calibration-independent)'
                             .format(ratio['count_ratio']))
            ratio_remark = (
                '    <Remark>Mixed field Cs-137 : Co-60 (as Cs-137 / Co-60): '
                '{parts}. Co-60 is the PRIMARY quantitative reference (two '
                'self-consistent lines, and unaffected by Cs-137); Cs-137 is '
                'SECONDARY - its 662 keV peak sits on the Co-60 Compton continuum, '
                'which raises its background and MDA.</Remark>\n'.format(
                    parts=', '.join(parts)))

        return (
            '  <AnalysisResults{ref}>\n'
            '    <Remark>Activities derived from the accumulated spectrum of this '
            'measurement. Detected nuclides quote the EXPANDED uncertainty (k=2, '
            '95%), combining per-run counting statistics with the systematic assay '
            'uncertainty ({us:.1f}% 1sigma, dominated by source position within the '
            'tray); it is NOT counting statistics alone. Non-detected configured '
            'nuclides carry a Currie MDA.</Remark>\n'
            '{ratio}'
            '{screening_remark}'
            '    <NuclideAnalysisResults>\n{nuclides}\n'
            '    </NuclideAnalysisResults>\n'
            '    <PeakAnalysisResults>\n{peaks}\n'
            '    </PeakAnalysisResults>\n'
            '  </AnalysisResults>\n'.format(
                ref=ref, us=u_sys, ratio=ratio_remark,
                screening_remark=screening_remark,
                nuclides="\n".join(nuclide_xml), peaks="\n".join(peak_xml)))

    def _save_n42(self, prefix, spectrum, real_time_ms, live_time_ms,
                  measurement_id="", reference_datetime="", activities=None,
                  id_results=None, unknown_peaks=None, dt_factor=1.0,
                  background_rates=None):
        """Save spectrum in ANSI N42.42 XML format.

        The measurement_id is stamped onto <RadMeasurement id=...> so the raw
        N42 asset is linkable back to the parent Measurement (spec DB-GEGI-004).
        When activity results are supplied they are embedded as <AnalysisResults>,
        making the N42 a complete, self-describing record of the measurement.
        """
        filepath = os.path.join(self.output_dir, "{}_spectrum.n42".format(prefix))

        real_time_s = real_time_ms / 1000.0
        live_time_s = live_time_ms / 1000.0

        # Energy calibration coefficients (linear fit to bin edges)
        # Channel to energy: E = offset + gain * channel
        if len(self.bin_edges) >= 2:
            offset = self.bin_edges[0]
            gain = (self.bin_edges[-1] - self.bin_edges[0]) / (len(self.bin_edges) - 1)
        else:
            offset = 0.0
            gain = 3.0

        # N42 spectrum data as space-separated counts
        spectrum_text = " ".join(str(int(c)) for c in spectrum)

        n42_xml = """<?xml version="1.0" encoding="UTF-8"?>
<RadInstrumentData xmlns="http://physics.nist.gov/N42/2011/N42"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                   xsi:schemaLocation="http://physics.nist.gov/N42/2011/N42 http://physics.nist.gov/N42/2011/n42.xsd">
  <RadInstrumentInformation>
    <RadInstrumentManufacturerName>PHDS</RadInstrumentManufacturerName>
    <RadInstrumentModelName>GeGI</RadInstrumentModelName>
    <RadInstrumentClassCode>Spectroscopic Personal Radiation Detector</RadInstrumentClassCode>
    <RadInstrumentVersion>
      <RadInstrumentComponentName>Software</RadInstrumentComponentName>
      <RadInstrumentComponentVersion>prism_phds_gegi_driver</RadInstrumentComponentVersion>
    </RadInstrumentVersion>
  </RadInstrumentInformation>
  <RadDetectorInformation>
    <RadDetectorName>GeGI_HPGe</RadDetectorName>
    <RadDetectorCategoryCode>Gamma</RadDetectorCategoryCode>
    <RadDetectorKindCode>HPGe</RadDetectorKindCode>
    <RadDetectorDescription>PHDS GeGI HPGe detector, 90mm dia, 11mm thick</RadDetectorDescription>
  </RadDetectorInformation>
  <EnergyCalibration>
    <CoefficientValues>{offset:.6f} {gain:.6f} 0.0</CoefficientValues>
  </EnergyCalibration>
  <RadMeasurement id="{measurement_id}">
    <MeasurementClassCode>Foreground</MeasurementClassCode>
    <StartDateTime>{reference_datetime}</StartDateTime>
    <RealTimeDuration>PT{real_time:.3f}S</RealTimeDuration>
    <Spectrum>
      <LiveTimeDuration>PT{live_time:.3f}S</LiveTimeDuration>
      <ChannelData compressionCode="None">{spectrum_data}</ChannelData>
    </Spectrum>
  </RadMeasurement>
{analysis_results}</RadInstrumentData>
""".format(
            offset=offset,
            gain=gain,
            real_time=real_time_s,
            live_time=live_time_s,
            spectrum_data=spectrum_text,
            measurement_id=measurement_id,
            reference_datetime=reference_datetime,
            analysis_results=self._build_analysis_results_xml(
                activities or [], measurement_id, id_results, unknown_peaks,
                dt_factor, background_rates)
        )

        with open(filepath, 'w') as f:
            f.write(n42_xml)
        logger.info("  N42 saved: %s", filepath)

    def _save_heatmap_csv(self, prefix, cloud):
        """Save full sphere heatmap as CSV (raw irregular points, background-subtracted)."""
        filepath = os.path.join(self.output_dir, "{}_heatmap_raw.csv".format(prefix))

        with open(filepath, 'w') as f:
            writer = csv.writer(f)
            writer.writerow(["x", "y", "z", "intensity_uSv_h", "cs137_uSv_h", "co60_uSv_h"])
            if cloud is not None and len(cloud) > 0:
                # Background subtraction: remove the floor (5th percentile)
                intensity = cloud[:, 3].copy()
                cs137 = cloud[:, 4].copy()
                co60 = cloud[:, 5].copy()

                floor_i = np.percentile(intensity, 5) if len(intensity) > 0 else 0.0
                floor_c = np.percentile(cs137, 5) if len(cs137) > 0 else 0.0
                floor_o = np.percentile(co60, 5) if len(co60) > 0 else 0.0

                intensity = np.maximum(intensity - floor_i, 0.0)
                cs137 = np.maximum(cs137 - floor_c, 0.0)
                co60 = np.maximum(co60 - floor_o, 0.0)

                # Only write points above 5% of max (skip the zero-background majority)
                max_val = intensity.max() if intensity.max() > 0 else 1.0
                threshold = max_val * 0.05

                count = 0
                for i in range(len(cloud)):
                    if intensity[i] >= threshold:
                        writer.writerow(["{:.6f}".format(cloud[i, 0]),
                                         "{:.6f}".format(cloud[i, 1]),
                                         "{:.6f}".format(cloud[i, 2]),
                                         "{:.6f}".format(intensity[i]),
                                         "{:.6f}".format(cs137[i]),
                                         "{:.6f}".format(co60[i])])
                        count += 1
                logger.info("  Heatmap raw CSV saved: %s (%d/%d points above threshold)",
                            filepath, count, len(cloud))
            else:
                logger.warning("  Heatmap raw CSV saved (empty - no cloud data received)")

    @staticmethod
    def _gaussian_smooth(grid, sigma=3.0):
        """2D Gaussian smoothing - same as live 2D heatmap."""
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(grid, sigma=sigma)

    def _save_heatmap_raster(self, prefix, cloud, peaks=None):
        """Save rasterised heatmap on regular 5mm Y-Z grid.

        Uses the same algorithm as the live 2D heatmap: per-isotope grid
        projection with tight Gaussian blobs around detected peaks for
        clean source separation.
        """
        filepath = os.path.join(self.output_dir, "{}_heatmap_raster.csv".format(prefix))
        cell = 0.005  # 5mm cells
        radius = self.source_distance_m  # sphere radius
        fov = radius * 1.1  # slightly larger than sphere radius

        if cloud is None or len(cloud) == 0:
            with open(filepath, 'w') as f:
                f.write("x,y,z,intensity_uSv_h,cs137_uSv_h,co60_uSv_h\n")
            logger.warning("  Heatmap raster CSV saved (empty)")
            return

        from scipy.ndimage import gaussian_filter

        res = 200  # same as live heatmap
        extent = 0.2  # +/-200mm (400mm x 400mm)
        y_edges = np.linspace(-extent, extent, res + 1)
        z_edges = np.linspace(-extent, extent, res + 1)
        yc = np.linspace(-extent, extent, res)
        zc = np.linspace(-extent, extent, res)
        YY, ZZ = np.meshgrid(yc, zc)
        r2 = YY**2 + ZZ**2
        R2 = radius**2

        # Extract sphere coordinates and scores
        points_yz = cloud[:, 1:3]  # Y, Z
        intensity = cloud[:, 3]
        cs137_scores = cloud[:, 4]
        co60_scores = cloud[:, 5]

        # Background subtraction
        valid_mask = intensity > 0
        if valid_mask.any():
            floor_i = np.median(intensity[valid_mask])
            floor_c = np.median(cs137_scores[valid_mask])
            floor_o = np.median(co60_scores[valid_mask])
        else:
            floor_i = floor_c = floor_o = 0.0

        intensity_sub = np.maximum(intensity - floor_i, 0.0)
        cs137_sub = np.maximum(cs137_scores - floor_c, 0.0)
        co60_sub = np.maximum(co60_scores - floor_o, 0.0)

        def _project_to_2d_grid(scores):
            """Identical to plot_live_2d_heatmap._project_to_2d_grid."""
            grid = np.zeros((res, res), dtype=np.float64)
            counts = np.zeros((res, res), dtype=np.float64)
            y_idx = np.digitize(cloud[:, 1], y_edges) - 1
            z_idx = np.digitize(cloud[:, 2], z_edges) - 1
            valid = (y_idx >= 0) & (y_idx < res) & (z_idx >= 0) & (z_idx < res)
            for i in range(len(cloud)):
                if valid[i]:
                    grid[z_idx[i], y_idx[i]] += scores[i]
                    counts[z_idx[i], y_idx[i]] += 1.0
            mask = counts > 0
            grid[mask] /= counts[mask]
            grid = gaussian_filter(grid, sigma=5.0)
            cos_factor = np.sqrt(np.clip(1.0 - r2 / R2, 0.0, 1.0))
            grid *= cos_factor
            grid[r2 > R2] = 0.0
            return grid

        grid_cs137 = _project_to_2d_grid(cs137_sub)
        grid_co60 = _project_to_2d_grid(co60_sub)

        # Per-peak pure Gaussian blobs scaled by smoothed grid intensity
        if peaks and len(peaks) > 0:
            sigma_blob = 0.05  # 50mm - smooth blob matching live heatmap appearance

            grid_intensity = np.zeros((res, res), dtype=np.float64)

            for peak in peaks:
                py, pz = peak[1], peak[2]
                iso_name = peak[3] if len(peak) > 3 else ''

                # Get amplitude from the smoothed grid at peak pixel position
                peak_yi = int(np.clip((py + extent) / (2.0 * extent) * res, 0, res - 1))
                peak_zi = int(np.clip((pz + extent) / (2.0 * extent) * res, 0, res - 1))

                if 'Cs-137' in iso_name or 'Cs137' in iso_name:
                    amp = float(grid_cs137[peak_zi, peak_yi])
                elif 'Co-60' in iso_name or 'Co60' in iso_name:
                    amp = float(grid_co60[peak_zi, peak_yi])
                else:
                    amp = float(max(grid_cs137[peak_zi, peak_yi],
                                    grid_co60[peak_zi, peak_yi]))
                if amp < 1e-12:
                    continue

                # Pure Gaussian blob - no grid multiplication
                dist = np.sqrt((YY - py)**2 + (ZZ - pz)**2)
                blob = amp * np.exp(-0.5 * (dist / sigma_blob)**2)

                grid_intensity = np.maximum(grid_intensity, blob)
        else:
            grid_intensity = _project_to_2d_grid(intensity_sub)

        # Write CSV
        count = 0
        x_coord = radius
        with open(filepath, 'w') as f:
            f.write("x,y,z,intensity\n")
            for zi in range(res):
                for yi in range(res):
                    f.write("{:.4f},{:.4f},{:.4f},{:.6f}\n".format(
                        x_coord, yc[yi], zc[zi],
                        grid_intensity[zi, yi]))
                    count += 1

        logger.info("  Heatmap raster CSV saved: %s (%d cells, res=%d)",
                    filepath, count, res)

    def _save_heatmap_3d(self, prefix, cloud, peaks):
        """Save 3D heatmap: localized blobs on a flat Y-Z plane at x=distance.

        Projects sphere back-projection scores onto a 0.5m x 0.5m plane using
        ray-plane intersection (Y_real = distance * Y_sphere / X_sphere), then
        applies Gaussian-weighted blobs around detected peaks.

        Output: x, y, z, dose_rate_uSv_h (only points above 10% of peak)
        Suitable for machine vision / robot picking.
        """
        filepath = os.path.join(self.output_dir, "{}_heatmap_3d.csv".format(prefix))
        distance = self.source_distance_m
        res = self.heatmap_grid_res
        blob_radius = 0.10  # metres - tight blob for good peak separation

        if cloud is None or len(cloud) == 0:
            with open(filepath, 'w') as f:
                writer = csv.writer(f)
                writer.writerow(["x", "y", "z", "dose_rate_uSv_h"])
            logger.warning("  Heatmap 3D CSV saved (empty - no cloud data)")
            return

        # Fixed 0.5m x 0.5m plane: Y in [-0.5, 0.5], Z in [-0.5, 0.5]
        extent = 0.5
        y_edges = np.linspace(-extent, extent, res + 1)
        z_edges = np.linspace(-extent, extent, res + 1)
        y_centers = 0.5 * (y_edges[:-1] + y_edges[1:])
        z_centers = 0.5 * (z_edges[:-1] + z_edges[1:])
        YY, ZZ = np.meshgrid(y_centers, z_centers)

        # Step 1: Convert sphere points to real-world Y-Z via ray-plane intersection
        # Y_real = distance * Y_sphere / X_sphere
        pts_x = cloud[:, 0]
        pts_y = cloud[:, 1]
        pts_z = cloud[:, 2]
        scores = cloud[:, 3]  # intensity column

        # Only use points with positive X (forward hemisphere)
        valid_x = pts_x > 1e-6
        real_y = np.where(valid_x, distance * pts_y / pts_x, 0.0)
        real_z = np.where(valid_x, distance * pts_z / pts_x, 0.0)

        # Bin onto Y-Z grid
        grid_sum = np.zeros((res, res), dtype=np.float64)
        grid_count = np.zeros((res, res), dtype=np.float64)

        y_idx = np.digitize(real_y, y_edges) - 1
        z_idx = np.digitize(real_z, z_edges) - 1
        valid = valid_x & (y_idx >= 0) & (y_idx < res) & (z_idx >= 0) & (z_idx < res)

        for i in range(len(cloud)):
            if valid[i]:
                grid_sum[z_idx[i], y_idx[i]] += scores[i]
                grid_count[z_idx[i], y_idx[i]] += 1.0

        base_grid = np.zeros((res, res), dtype=np.float64)
        mask = grid_count > 0
        base_grid[mask] = grid_sum[mask] / grid_count[mask]

        # Smooth the base grid (3x3 average, 3 passes)
        for _ in range(3):
            padded = np.pad(base_grid, 1, mode='constant')
            base_grid = (padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:] +
                         padded[1:-1, :-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:] +
                         padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:]) / 9.0

        # Step 2: Extract unique peaks (already in real-world coordinates)
        unique_peaks = {}
        for entry in peaks:
            # entry = (x, y, z, isotope_name, activity_MBq)
            iso_name = entry[3]
            unique_peaks[iso_name] = (entry[1], entry[2])  # (py, pz) real-world

        if not unique_peaks:
            logger.warning("  Heatmap 3D: no peaks detected, saving empty file")
            with open(filepath, 'w') as f:
                writer = csv.writer(f)
                writer.writerow(["x", "y", "z", "dose_rate_uSv_h"])
            return

        # Step 3: For each peak, create Gaussian-weighted blob from base grid
        grid = np.zeros((res, res), dtype=np.float64)
        sigma = blob_radius * 0.4

        # Use dose rate for amplitude weighting: D_dot = Gamma * A / d^2
        d = self.source_distance_m
        dose_rates = {}
        for entry in peaks:
            iso_name = entry[3]
            activity_mbq = entry[4] if len(entry) > 4 else 0.0
            gamma = gamma_constant_for(self.gamma_constants, iso_name)
            dr = gamma * activity_mbq / (d * d) if d > 0 else 0.0
            if iso_name in dose_rates:
                dose_rates[iso_name] = max(dose_rates[iso_name], dr)
            else:
                dose_rates[iso_name] = dr
        max_rate = max(dose_rates.values()) if dose_rates else 1.0
        if max_rate < 1e-9:
            max_rate = 1.0

        for iso_name, (py, pz) in unique_peaks.items():
            amp = dose_rates.get(iso_name, 0.0) / max_rate
            if amp < 0.1:
                amp = 0.5  # fallback if no activity data
            dist = np.sqrt((YY - py)**2 + (ZZ - pz)**2)
            falloff = np.exp(-0.5 * (dist / sigma)**2)
            blob = falloff * base_grid * amp
            grid = np.maximum(grid, blob)

        # Intensity values are dose-rate-weighted (not normalized)

        # Write only hot points (above 10% of peak value)
        grid_max = grid.max()
        threshold = grid_max * 0.10 if grid_max > 1e-12 else 0.0
        count = 0
        with open(filepath, 'w') as f:
            writer = csv.writer(f)
            writer.writerow(["x", "y", "z", "dose_rate_uSv_h"])
            for zi in range(res):
                for yi in range(res):
                    val = grid[zi, yi]
                    if val >= threshold:
                        writer.writerow([
                            "{:.4f}".format(distance),
                            "{:.6f}".format(y_centers[yi]),
                            "{:.6f}".format(z_centers[zi]),
                            "{:.6f}".format(val)
                        ])
                        count += 1

        logger.info("  Heatmap 3D CSV saved: %s (%d hot points of %dx%d grid, %d peaks, x=%.2fm)",
                    filepath, count, res, res, len(unique_peaks), distance)

    # Map the driver's internal isotope/line labels to controlled radionuclide
    # names for the database ActivityResult (spec DB-ACT-001, sec 7.3). The internal
    # per-line label (e.g. "Co60_1332") is kept separately in the `isotope`
    # column; `radionuclide` carries the controlled source identity.
    _RADIONUCLIDE_MAP = {
        'Cs137': 'Cs-137',
        'Co60': 'Co-60',
        'Co60_1173': 'Co-60',
        'Co60_1332': 'Co-60',
    }

    @classmethod
    def _controlled_radionuclide(cls, name):
        if name in cls._RADIONUCLIDE_MAP:
            return cls._RADIONUCLIDE_MAP[name]
        if 'Cs137' in name or 'Cs-137' in name:
            return 'Cs-137'
        if 'Co60' in name or 'Co-60' in name:
            return 'Co-60'
        return name

    def _write_background(self, activities, measurement_id):
        """Write this (no-source) run's per-line net rates as the background
        reference used to subtract from later runs. Also updates the in-memory
        rates so the very next run subtracts without a relaunch.
        """
        rates = build_background_rates(activities)
        doc = {
            'source_measurement_id': measurement_id,
            'note': ('Per-line background NET rate (cps) for subtraction. '
                     'Re-record on a no-source run with --record-background.'),
            'lines': rates,
        }
        try:
            with open(self.background_file, 'w') as f:
                yaml.safe_dump(doc, f, default_flow_style=False)
            self.background_rates = rates
            logger.info("  Background reference written: %s (%d lines)",
                        self.background_file, len(rates))
        except Exception as e:
            logger.error("Failed to write background reference: %s", e)

    def _save_activity_csv(self, prefix, activities, detector_run_info,
                           run_real_time_ms=0, run_live_time_ms=0,
                           measurement_id="", reference_datetime=""):
        """Save activity measurement results as CSV.

        Columns are limited to what activity estimation needs:
          - live_time_s: PER-window integration time used for the count rate.
          - run_real_time_s / run_live_time_s: whole-run totals (scan duration).
          - detector_run_*: authoritative hardware real/live/dead-time for the run.
        The per-window real_time_s / dead_time_fraction / dead_time_status columns
        were dropped: with live dead-time polling off they duplicated live_time_s
        and read a constant zero; the real dead time is in detector_run_*.

        Provenance columns (measurement_id, measurement_basis, activity_unit,
        reference_datetime, radionuclide) satisfy spec DB-GEGI-002/004 and
        DB-ACT-001/002/003 so each processed peak row links to one parent
        Measurement and never loses its unit/reference-date/basis.
        """
        filepath = os.path.join(self.output_dir, "{}_activity.csv".format(prefix))

        run_real_time_s = run_real_time_ms / 1000.0
        run_live_time_s = run_live_time_ms / 1000.0

        with open(filepath, 'w') as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp_s", "live_time_s",
                "detector_run_info_valid", "detector_run_dead_time_percent",
                "detector_run_real_time_s", "detector_run_live_time_s",
                "isotope", "energy_keV", "gross_counts", "background_counts",
                "net_peak_area", "net_corrected", "count_rate_cps",
                "activity_MBq", "sigma_activity_MBq", "valid",
                "run_real_time_s", "run_live_time_s",
                "measurement_id", "radionuclide", "measurement_basis",
                "activity_unit", "reference_datetime"
            ])

            for report in activities:
                ts = report.get('timestamp', 0)
                lt = report.get('live_time_s', 0)
                drv = detector_run_info.get('valid', False)
                drdt = detector_run_info.get('dead_time_percent', 0.0)
                drrt = detector_run_info.get('real_time_sec', 0.0)
                drlt = detector_run_info.get('live_time_sec', 0.0)

                for iso in report.get('isotopes', []):
                    iso_name = iso.get('isotope', '')
                    writer.writerow([
                        "{:.3f}".format(ts),
                        "{:.3f}".format(lt),
                        drv,
                        "{:.6f}".format(drdt),
                        "{:.3f}".format(drrt),
                        "{:.3f}".format(drlt),
                        iso_name,
                        "{:.1f}".format(iso.get('energy_keV', 0)),
                        "{:.0f}".format(iso.get('gross_counts', 0)),
                        "{:.1f}".format(iso.get('background_counts', 0)),
                        "{:.1f}".format(iso.get('net_peak_area', 0)),
                        "{:.1f}".format(iso.get('net_corrected', 0)),
                        "{:.4f}".format(iso.get('count_rate_cps', 0)),
                        "{:.6f}".format(iso.get('activity_MBq', 0)),
                        "{:.6f}".format(iso.get('sigma_activity_MBq', 0)),
                        iso.get('valid', False),
                        "{:.3f}".format(run_real_time_s),
                        "{:.3f}".format(run_live_time_s),
                        measurement_id,
                        self._controlled_radionuclide(iso_name),
                        "direct_measured",
                        "MBq",
                        reference_datetime
                    ])

        logger.info("  Activity CSV saved: %s (%d measurement windows)",
                    filepath, len(activities))

    def _save_manifest(self, prefix, measurement_id, reference_datetime,
                       duration_minutes, run_real_time_ms, run_live_time_ms,
                       detector_run_info, activities):
        """Write a per-run manifest linking every asset to one Measurement.

        Anchors the database Measurement record (spec DB-GEGI-002/004): captures
        the acquisition/processing configuration and the list of raw and
        processed assets produced by this run.
        """
        filepath = os.path.join(self.output_dir, "{}_manifest.json".format(prefix))

        # Candidate assets with their database role; only list those written.
        candidates = [
            ("{}_spectrum.n42".format(prefix), "raw_spectrum_n42", "N42"),
            ("{}_compton_events.jsonl".format(prefix), "raw_compton_events", "jsonl"),
            ("{}_activity.csv".format(prefix), "processed_peak_results", "CSV"),
            ("{}_heatmap_raw.csv".format(prefix), "gamma_image_raw", "CSV"),
            ("{}_heatmap_raster.csv".format(prefix), "gamma_image_raster", "CSV"),
            ("{}_heatmap_3d.csv".format(prefix), "gamma_image_3d", "CSV"),
        ]
        assets = []
        for fname, role, kind in candidates:
            fpath = os.path.join(self.output_dir, fname)
            if os.path.exists(fpath):
                assets.append({
                    "file": fname,
                    "role": role,
                    "format": kind,
                    "size_bytes": os.path.getsize(fpath),
                })

        # Distinct radionuclides seen in the processed results.
        radionuclides = []
        for report in activities:
            for iso in report.get('isotopes', []):
                rn = self._controlled_radionuclide(iso.get('isotope', ''))
                if rn and rn not in radionuclides:
                    radionuclides.append(rn)

        manifest = {
            "measurement_id": measurement_id,
            "measurement_type": "gamma_spectroscopy+gamma_imaging",
            "reference_datetime": reference_datetime,
            "detector": {"manufacturer": "PHDS", "model": "GeGI", "kind": "HPGe"},
            "acquisition": {
                "requested_duration_minutes": duration_minutes,
                "run_real_time_s": run_real_time_ms / 1000.0,
                "run_live_time_s": run_live_time_ms / 1000.0,
                "source_distance_m": self.source_distance_m,
                "activity_basis": "direct_measured",
            },
            "processing_config": {
                "calibration_file": self.calibration_file,
                "isotopes_config": self.isotopes_config,
                "heatmap_grid_res": self.heatmap_grid_res,
            },
            "detector_run_info": detector_run_info,
            "radionuclides": radionuclides,
            "assets": assets,
        }

        with open(filepath, 'w') as f:
            json.dump(manifest, f, indent=2, sort_keys=True)

        logger.info("  Manifest saved: %s (%d assets, id=%s)",
                    filepath, len(assets), measurement_id)


def main():
    app = prism.Application("data_recorder_node",
                             "GeGi timed-acquisition data recorder", sys.argv)

    app.add_string_option("Recorder", "output-dir", "Directory for saved files",
                          "/opt/phds_gegi_driver/data")
    app.add_string_option("Recorder", "calibration-file", "Path to EnergyCal.csv (energy bin edges in keV)", "")
    app.add_string_option("Recorder", "isotopes-config", "Path to isotopes.yaml configuration", "")
    app.add_float_option("Recorder", "source-distance-m", "Default source-detector distance in metres", 0.5)
    app.add_int_option("Recorder", "heatmap-grid-res", "3D heatmap grid resolution", 200)
    app.add_float_option("Recorder", "run-info-settle-s",
                         "(unused, kept for config parity) run-info settle time in seconds", 15.0)
    app.add_float_option("Recorder", "run-info-poll-s",
                         "(unused, kept for config parity) run-info poll interval in seconds", 30.0)
    app.add_string_option("Recorder", "measurement-id-prefix",
                          "Prefix for the durable measurement identifier", "GEGI")
    app.add_float_option("Recorder", "assay-systematic-uncertainty-percent",
                         "Systematic (non-counting) assay uncertainty, 1 sigma, in percent", 10.6)
    app.add_bool_option("Recorder", "apply-dead-time-correction",
                        "Apply detector dead-time (real/live) correction to run-aggregated "
                        "net areas/activities. OFF by default -- requires isotopes.yaml "
                        "calibration factors re-derived from dead-time-corrected data first, "
                        "see dead_time_correction_factor()")
    app.add_bool_option("Recorder", "no-report-non-detected-mda",
                        "Disable Currie MDA reporting for configured-but-not-detected "
                        "nuclides in the N42 (enabled by default)")
    app.add_string_option("Recorder", "background-file",
                          "Path to a background reference YAML (per-line net rates) written "
                          "by --record-background; empty = <output-dir>/background.yaml", "")
    app.add_bool_option("Recorder", "record-background",
                        "Record this (no-source) run as the background reference instead of "
                        "subtracting it from a source run")
    app.add_string_option("Recorder", "node-clear-targets",
                          "Comma-separated list of topic:command pairs to fan out clear_all to",
                          DEFAULT_NODE_CLEAR_TARGETS)
    app.add_string_option("Recorder", "node-name",
                          "Used to build gegi.<node-name>.command(_result) topic names",
                          "data_recorder")
    app.add_string_option("Recorder", "nuclide-library",
                          "Path to nuclide_library.yaml for the isotope-ID screening layer "
                          "(empty = screening disabled)", "")
    app.add_float_option("Recorder", "isotope-id-period-s",
                         "Screening pass interval over the rolling live window, seconds", 20.0)
    app.add_float_option("Recorder", "isotope-id-window-s",
                         "Rolling live-spectrum window summed for continuous screening, seconds",
                         180.0)

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    connection = app.create_connection(result)
    if connection is None:
        logger.error("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    node = DataRecorderNode(app, result, connection)

    while app.is_running():
        time.sleep(0.1)

    node.stop()
    connection.close()


if __name__ == "__main__":
    main()
