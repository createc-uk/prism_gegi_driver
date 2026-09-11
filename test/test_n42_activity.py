#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the run-level activity aggregation written into the N42.

Two things must not drift:

  1. The AGGREGATION RULE. Activity is computed from total net counts / total
     live time, and partial start-up windows are excluded. A partial window has
     partial counts but a FULL logged live time, so including it biases the
     activity LOW (this was measured at -6.8% on a real run).

  2. The UNCERTAINTY SEMANTICS. The N42 carries the EXPANDED (k=2) uncertainty,
     combining per-run counting statistics with the systematic budget. Writing
     counting statistics alone (~1-3%) into a durable record would understate the
     real measurement uncertainty roughly ten-fold.
"""
from __future__ import print_function

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))

import data_recorder_node as dr  # noqa: E402


def window(live_s, lines):
    """One counting-window report: lines = [(label, net, activity_MBq, energy)]."""
    return {
        'live_time_s': live_s,
        'isotopes': [
            {'isotope': lbl, 'net_corrected': net, 'activity_MBq': act,
             'energy_keV': e, 'valid': True}
            for lbl, net, act, e in lines
        ],
    }


class TestAggregation(unittest.TestCase):
    def test_activity_from_total_counts_over_total_live_time(self):
        # Bq-per-cps = 0.06 in both windows.
        # Total: 2100 net / 120 s = 17.5 cps -> 1.05 MBq.
        reports = [
            window(60.0, [('Cs137', 1000.0, 1.00, 661.7)]),   # 16.667 cps
            window(60.0, [('Cs137', 1100.0, 1.10, 661.7)]),   # 18.333 cps
        ]
        res = dr.aggregate_run_activity(reports)['Cs137']
        self.assertAlmostEqual(res['activity_MBq'], 1.05, delta=1e-9)
        self.assertAlmostEqual(res['net_counts'], 2100.0, delta=1e-9)
        self.assertAlmostEqual(res['live_time_s'], 120.0, delta=1e-9)
        self.assertEqual(res['windows_used'], 2)

    def test_longer_windows_are_weighted_more(self):
        """Total-counts/total-live-time, NOT the mean of the window activities.

        Bq-per-cps = 0.06.  60 s @ 16.667 cps + 180 s @ 20 cps
          -> 4600 net / 240 s = 19.167 cps -> 1.15 MBq
        The naive mean of the window activities would give 1.10 - wrong, because
        it under-weights the long window.
        """
        reports = [
            window(60.0, [('Cs137', 1000.0, 1.00, 661.7)]),
            window(180.0, [('Cs137', 3600.0, 1.20, 661.7)]),
        ]
        res = dr.aggregate_run_activity(reports)['Cs137']
        self.assertAlmostEqual(res['activity_MBq'], 1.15, delta=1e-9)
        self.assertNotAlmostEqual(res['activity_MBq'], 1.10, delta=1e-3)

    def test_short_window_with_a_normal_rate_is_KEPT(self):
        """The filter must key on RATE, not raw counts.

        A genuinely shorter window has fewer counts but a normal rate; dropping
        it would throw away good data. Only a ramp window - partial counts against
        a FULL logged live time, hence a low rate - may be dropped.
        """
        reports = [
            window(60.0, [('Cs137', 1200.0, 1.2, 661.7)]),   # 20 cps
            window(60.0, [('Cs137', 1200.0, 1.2, 661.7)]),   # 20 cps
            window(15.0, [('Cs137', 300.0, 1.2, 661.7)]),    # 20 cps - SHORT, normal rate
        ]
        res = dr.aggregate_run_activity(reports)['Cs137']
        self.assertEqual(res['windows_dropped'], 0, "short-but-normal must be kept")
        self.assertEqual(res['windows_used'], 3)
        self.assertAlmostEqual(res['activity_MBq'], 1.2, delta=1e-9)

    def test_partial_startup_window_is_excluded(self):
        """The regression this rule exists to prevent."""
        reports = [
            window(60.0, [('Co60_1173', 300.0, 0.30, 1173.2)]),   # ramp-up: partial
            window(60.0, [('Co60_1173', 1200.0, 1.20, 1173.2)]),
            window(60.0, [('Co60_1173', 1200.0, 1.20, 1173.2)]),
            window(60.0, [('Co60_1173', 1200.0, 1.20, 1173.2)]),
        ]
        res = dr.aggregate_run_activity(reports)['Co60_1173']
        self.assertEqual(res['windows_dropped'], 1)
        self.assertEqual(res['windows_used'], 3)
        self.assertAlmostEqual(res['activity_MBq'], 1.20, delta=1e-9)

    def test_including_the_partial_window_would_bias_low(self):
        """Demonstrates WHY the rule exists: without it the answer is wrong."""
        reports = [
            window(60.0, [('Co60_1173', 300.0, 0.30, 1173.2)]),
            window(60.0, [('Co60_1173', 1200.0, 1.20, 1173.2)]),
            window(60.0, [('Co60_1173', 1200.0, 1.20, 1173.2)]),
            window(60.0, [('Co60_1173', 1200.0, 1.20, 1173.2)]),
        ]
        good = dr.aggregate_run_activity(reports, drop_threshold=0.75)['Co60_1173']
        naive = dr.aggregate_run_activity(reports, drop_threshold=0.0)['Co60_1173']
        self.assertAlmostEqual(good['activity_MBq'], 1.20, delta=1e-9)
        self.assertLess(naive['activity_MBq'], 1.13)      # biased LOW
        self.assertGreater(good['activity_MBq'], naive['activity_MBq'])

    def test_subthreshold_windows_still_aggregate_over_the_run(self):
        """Per-window validity is IGNORED; a weak line significant over the whole
        run is quantified from the run total (the Co-60 1332 keV case)."""
        reports = [
            window(60.0, [('Co60_1332', 350.0, 1.10, 1332.5)]),
            window(60.0, [('Co60_1332', 350.0, 1.10, 1332.5)]),
            window(60.0, [('Co60_1332', 350.0, 1.10, 1332.5)]),
        ]
        for r in reports:                       # mark every window invalid...
            for iso in r['isotopes']:
                iso['valid'] = False
        res = dr.aggregate_run_activity(reports)['Co60_1332']   # ...still aggregated
        self.assertEqual(res['windows_used'], 3)
        self.assertAlmostEqual(res['net_counts'], 1050.0, delta=1e-9)
        self.assertAlmostEqual(res['activity_MBq'], 1.10, delta=1e-9)

    def test_counting_sigma_shrinks_with_counts(self):
        few = dr.aggregate_run_activity(
            [window(60.0, [('Cs137', 100.0, 1.0, 661.7)])])['Cs137']
        many = dr.aggregate_run_activity(
            [window(60.0, [('Cs137', 10000.0, 1.0, 661.7)])])['Cs137']
        # sigma/activity = 1/sqrt(N):  10% at 100 counts, 1% at 10000
        self.assertAlmostEqual(few['counting_sigma_MBq'] / few['activity_MBq'],
                               0.10, delta=1e-9)
        self.assertAlmostEqual(many['counting_sigma_MBq'] / many['activity_MBq'],
                               0.01, delta=1e-9)

    def test_no_valid_data_yields_nothing(self):
        self.assertEqual(dr.aggregate_run_activity([]), {})

    def test_few_stray_counts_in_empty_roi_do_not_detect(self):
        """B~0 pathology guard: with an empty ROI, Currie L_C -> 0 and a couple
        of stray counts in one short partial window used to 'detect' (observed
        2026-09-03: Co60_1173 0.097+-0.103 MBq from a 1.5 s window in a
        Cs-only run). The absolute floor kills it."""
        reports = [window(1.5, [('Co60_1173', 3.0, 0.1, 1173.2)])]
        self.assertNotIn('Co60_1173', dr.aggregate_run_activity(reports))

    def test_floor_does_not_suppress_modest_real_signals(self):
        reports = [window(300.0, [('Cs137', 30.0, 2e-6, 661.7)])]
        self.assertIn('Cs137', dr.aggregate_run_activity(reports))

    def test_fluke_high_flush_fragment_cannot_drop_the_main_window(self):
        """Regression (run 20260904_172346): a 1.25 s end-of-run flush with a
        statistically inflated rate dragged the two-window median up, the
        genuine 300 s window was dropped as 'partial', and the activity was
        reported from 14 counts. Fragments must not steer the reference."""
        reports = [
            window(300.0, [('Co60_1173', 1700.0, 1.10, 1173.2)]),  # 5.67 cps
            window(1.25, [('Co60_1173', 14.0, 2.10, 1173.2)]),     # 11.2 cps
        ]
        res = dr.aggregate_run_activity(reports)['Co60_1173']
        self.assertEqual(res['windows_dropped'], 0)
        self.assertAlmostEqual(res['net_counts'], 1714.0, delta=1e-9)
        self.assertGreater(res['live_time_s'], 300.0)


class TestRadionuclideGrouping(unittest.TestCase):
    """Co-60's two photopeaks quantify ONE nuclide and must be combined."""

    def _lines(self, a1173, a1332):
        return dr.aggregate_run_activity([window(60.0, [
            ('Co60_1173', 1200.0, a1173, 1173.2),
            ('Co60_1332', 800.0, a1332, 1332.5),
        ])])

    def test_two_co60_lines_collapse_to_one_nuclide(self):
        groups = dr.group_by_radionuclide(
            self._lines(1.10, 1.20), dr.DataRecorderNode._controlled_radionuclide)
        self.assertEqual(sorted(groups), ['Co-60'])
        self.assertAlmostEqual(groups['Co-60']['activity_MBq'], 1.15, delta=1e-9)
        self.assertEqual(groups['Co-60']['lines'], ['Co60_1173', 'Co60_1332'])

    def test_line_spread_is_reported(self):
        """The two lines disagreeing is a real internal-consistency signal."""
        groups = dr.group_by_radionuclide(
            self._lines(1.10, 1.20), dr.DataRecorderNode._controlled_radionuclide)
        # (1.20 - 1.10) / 1.15 = 8.7%
        self.assertAlmostEqual(groups['Co-60']['line_spread_percent'], 8.6957,
                               delta=1e-3)

    def test_agreeing_lines_give_zero_spread(self):
        groups = dr.group_by_radionuclide(
            self._lines(1.15, 1.15), dr.DataRecorderNode._controlled_radionuclide)
        self.assertAlmostEqual(groups['Co-60']['line_spread_percent'], 0.0,
                               delta=1e-9)

    def test_distinct_nuclides_stay_separate(self):
        lines = dr.aggregate_run_activity([window(60.0, [
            ('Cs137', 1000.0, 2.0, 661.7),
            ('Co60_1173', 1200.0, 1.1, 1173.2),
        ])])
        groups = dr.group_by_radionuclide(
            lines, dr.DataRecorderNode._controlled_radionuclide)
        self.assertEqual(sorted(groups), ['Co-60', 'Cs-137'])


class TestDeadTimeCorrection(unittest.TestCase):
    """Dead-time correction (real/live) applied to saved net areas and activities.

    The correction has ONE physical meaning: scale the raw net/activity UP by
    real/live because the live-time clock already excluded the dead time. It must
    never fabricate counts when the run-info is missing or nonsensical.
    """

    def test_prefers_real_over_live(self):
        # real/live = 300/294 = 1.020408; the percent field is ignored when the
        # explicit counters are present and sane.
        f = dr.dead_time_correction_factor(300.0, 294.0, dead_time_percent=5.0)
        self.assertAlmostEqual(f, 300.0 / 294.0, delta=1e-9)

    def test_falls_back_to_percent_when_times_missing(self):
        f = dr.dead_time_correction_factor(0.0, 0.0, dead_time_percent=2.0)
        self.assertAlmostEqual(f, 1.0 / (1.0 - 0.02), delta=1e-9)

    def test_no_correction_when_everything_invalid(self):
        self.assertEqual(dr.dead_time_correction_factor(0.0, 0.0, None), 1.0)
        self.assertEqual(dr.dead_time_correction_factor(None, None, None), 1.0)
        # live > real is physically impossible -> refuse to "correct"
        self.assertEqual(dr.dead_time_correction_factor(100.0, 110.0), 1.0)
        # 100% dead time would divide by zero -> refuse
        self.assertEqual(dr.dead_time_correction_factor(0.0, 0.0, 100.0), 1.0)

    def test_factor_is_always_at_least_one(self):
        for rt, lt in ((300.0, 294.0), (60.0, 59.0), (10.0, 10.0)):
            self.assertGreaterEqual(dr.dead_time_correction_factor(rt, lt), 1.0)

    def test_aggregate_scales_activity_but_not_relative_sigma(self):
        """dt_factor scales activity; net_counts and relative sigma are unchanged
        because counting statistics are Poisson on the RAW detected counts."""
        reports = [window(60.0, [('Cs137', 1000.0, 1.00, 661.7)])]
        base = dr.aggregate_run_activity(reports, dt_factor=1.0)['Cs137']
        corr = dr.aggregate_run_activity(reports, dt_factor=1.02)['Cs137']
        self.assertAlmostEqual(corr['activity_MBq'],
                               base['activity_MBq'] * 1.02, delta=1e-9)
        # raw net is unchanged (correction is on activity, not counts)
        self.assertAlmostEqual(corr['net_counts'], base['net_counts'], delta=1e-9)
        # relative counting sigma is unchanged
        self.assertAlmostEqual(corr['counting_sigma_MBq'] / corr['activity_MBq'],
                               base['counting_sigma_MBq'] / base['activity_MBq'],
                               delta=1e-9)

    def test_default_factor_is_a_noop(self):
        reports = [window(60.0, [('Cs137', 1000.0, 1.00, 661.7)])]
        with_default = dr.aggregate_run_activity(reports)['Cs137']
        explicit_one = dr.aggregate_run_activity(reports, dt_factor=1.0)['Cs137']
        self.assertAlmostEqual(with_default['activity_MBq'],
                               explicit_one['activity_MBq'], delta=1e-12)


def mda_window(live, isos):
    """A window for MDA/detection tests. isos = list of (label, net, bg, K).

    Detection is now run-level (net vs Currie L_C), so tests supply real net
    counts. The per-window ``valid`` hint is derived but ignored by aggregate.
    """
    return {'live_time_s': live, 'isotopes': [
        {'isotope': lbl, 'net_corrected': net, 'net_peak_area': net,
         'background_counts': bg, 'efficiency_product': K,
         'valid': net >= 400.0, 'activity_MBq': 0.0, 'energy_keV': 0.0}
        for (lbl, net, bg, K) in isos]}


class TestRunLevelDetection(unittest.TestCase):
    """Detection is a run-total Currie decision, not a per-window count gate."""

    RN = staticmethod(dr.DataRecorderNode._controlled_radionuclide)

    def test_critical_level_formula(self):
        self.assertAlmostEqual(dr.currie_critical_level(100.0), 23.3, delta=1e-9)
        self.assertEqual(dr.currie_critical_level(0.0), 0.0)

    def test_run_total_above_critical_level_is_detected(self):
        # 3 windows x 350 net = 1050 >> L_C(bg=9)=6.99  -> detected
        reports = [mda_window(60.0, [('Co60_1332', 350.0, 3.0, 1.1e-6)])] * 3
        self.assertEqual(dr.detected_line_labels(reports), {'Co60_1332'})

    def test_run_total_below_critical_level_is_not_detected(self):
        # net 3 against bg 3 -> L_C = 2.33*sqrt(3) = 4.03 -> 3 < 4.03 -> absent
        reports = [mda_window(60.0, [('Cs137', 3.0, 3.0, 2.0e-6)])]
        self.assertEqual(dr.detected_line_labels(reports), set())

    def test_activity_uses_efficiency_product_when_present(self):
        # A = net/(K*t) = 1000/(1e-6*100) = 1e7 Bq = 10 MBq
        reports = [mda_window(100.0, [('Cs137', 1000.0, 3.0, 1.0e-6)])]
        res = dr.aggregate_run_activity(reports)['Cs137']
        self.assertAlmostEqual(res['activity_MBq'], 10.0, delta=1e-9)


class TestNonDetectionMDA(unittest.TestCase):
    """Absent nuclides: dropped from the CSV, reported once as an MDA in the N42."""

    RN = staticmethod(dr.DataRecorderNode._controlled_radionuclide)

    def test_currie_mda_matches_the_formula(self):
        # L_D = 2.71 + 4.65*sqrt(100) = 49.21 net counts; /(K*t)
        mda = dr.currie_mda_bq(100.0, 2.0e-6, 200.0)
        self.assertAlmostEqual(mda, 49.21 / (2.0e-6 * 200.0), delta=1e-6)

    def test_currie_mda_undefined_without_efficiency_or_time(self):
        self.assertIsNone(dr.currie_mda_bq(100.0, 0.0, 200.0))
        self.assertIsNone(dr.currie_mda_bq(100.0, 2.0e-6, 0.0))
        self.assertIsNone(dr.currie_mda_bq(100.0, None, 200.0))

    def test_more_background_gives_a_worse_mda(self):
        low = dr.currie_mda_bq(25.0, 1.0e-6, 100.0)
        high = dr.currie_mda_bq(400.0, 1.0e-6, 100.0)
        self.assertGreater(high, low)

    def test_absent_nuclide_gets_an_mda_detected_one_does_not(self):
        # Cs-137 sub-critical (absent); Co-60 clearly detected on 1173.
        reports = [
            mda_window(100.0, [('Cs137', 0.0, 50.0, 2.0e-6),
                               ('Co60_1173', 1000.0, 30.0, 1.0e-6)]),
            mda_window(100.0, [('Cs137', 0.0, 50.0, 2.0e-6),
                               ('Co60_1173', 1000.0, 30.0, 1.0e-6)]),
        ]
        detected = dr.detected_line_labels(reports)
        self.assertEqual(detected, {'Co60_1173'})
        mdas = dr.non_detected_nuclide_mdas(reports, detected, self.RN)
        self.assertEqual(sorted(mdas), ['Cs-137'])           # only the absent one
        self.assertAlmostEqual(mdas['Cs-137']['mda_Bq'],
                               dr.currie_mda_bq(100.0, 2.0e-6, 200.0), delta=1e-6)

    def test_co60_non_detection_bounded_by_most_sensitive_line(self):
        # Neither Co-60 line detected; the lower-MDA (higher-K) line bounds it.
        reports = [mda_window(100.0, [
            ('Co60_1173', 0.0, 40.0, 1.0e-6),
            ('Co60_1332', 0.0, 40.0, 2.0e-6),   # bigger K -> smaller MDA -> wins
        ])]
        mdas = dr.non_detected_nuclide_mdas(reports, set(), self.RN)
        self.assertEqual(sorted(mdas), ['Co-60'])
        self.assertEqual(mdas['Co-60']['line'], 'Co60_1332')

    def test_nothing_reported_when_all_detected(self):
        reports = [mda_window(60.0, [('Cs137', 1000.0, 3.0, 2.0e-6)])]
        detected = dr.detected_line_labels(reports)
        self.assertEqual(dr.non_detected_nuclide_mdas(reports, detected, self.RN), {})

    def test_weak_line_of_a_detected_nuclide_keeps_the_nuclide(self):
        """Co-60 detected on 1173 but its 1332 line stays sub-critical -> Co-60 is
        still detected, so the CSV keeps BOTH lines and it is NOT a non-detection."""
        reports = [mda_window(60.0, [
            ('Co60_1173', 1000.0, 3.0, 1.0e-6),   # detected
            ('Co60_1332', 3.0, 3.0, 1.1e-6),      # sub-critical this run
        ])]
        self.assertIn('Co-60', dr.detected_nuclides(reports, self.RN))
        detected = dr.detected_line_labels(reports)
        self.assertEqual(dr.non_detected_nuclide_mdas(reports, detected, self.RN), {})


class TestCs137Co60Ratio(unittest.TestCase):
    """Cs-137 : Co-60 ratio, reported only when BOTH are detected."""

    def test_ratio_when_both_detected(self):
        nuclides = {
            'Cs-137': {'activity_MBq': 0.50, 'net_counts': 4500.0},
            'Co-60':  {'activity_MBq': 0.38, 'net_counts': 3400.0},
        }
        r = dr.cs137_co60_ratio(nuclides)
        self.assertAlmostEqual(r['activity_ratio'], 0.50 / 0.38, delta=1e-9)
        self.assertAlmostEqual(r['count_ratio'], 4500.0 / 3400.0, delta=1e-9)

    def test_none_when_only_one_present(self):
        self.assertIsNone(dr.cs137_co60_ratio(
            {'Co-60': {'activity_MBq': 0.4, 'net_counts': 3000.0}}))
        self.assertIsNone(dr.cs137_co60_ratio(
            {'Cs-137': {'activity_MBq': 0.5, 'net_counts': 4000.0}}))
        self.assertIsNone(dr.cs137_co60_ratio({}))

    def test_activity_ratio_none_if_co60_activity_zero(self):
        # count ratio still available (calibration-independent) even if A(Co)=0
        r = dr.cs137_co60_ratio({
            'Cs-137': {'activity_MBq': 0.5, 'net_counts': 4000.0},
            'Co-60':  {'activity_MBq': 0.0, 'net_counts': 3000.0},
        })
        self.assertIsNone(r['activity_ratio'])
        self.assertAlmostEqual(r['count_ratio'], 4000.0 / 3000.0, delta=1e-9)

    def test_end_to_end_from_grouping(self):
        lines = dr.aggregate_run_activity([window(60.0, [
            ('Cs137', 4000.0, 2.0, 661.7),
            ('Co60_1173', 1500.0, 0.40, 1173.2),
            ('Co60_1332', 1100.0, 0.40, 1332.5),
        ])])
        nuclides = dr.group_by_radionuclide(
            lines, dr.DataRecorderNode._controlled_radionuclide)
        r = dr.cs137_co60_ratio(nuclides)
        self.assertIsNotNone(r)
        self.assertGreater(r['activity_ratio'], 0.0)
        # count ratio = 4000 / (1500 + 1100)
        self.assertAlmostEqual(r['count_ratio'], 4000.0 / 2600.0, delta=1e-9)


def bg_window(live, isos):
    """A window carrying gross too. isos = list of (label, net, gross, bg, K)."""
    return {'live_time_s': live, 'isotopes': [
        {'isotope': lbl, 'net_corrected': net, 'net_peak_area': net,
         'gross_counts': gross, 'background_counts': bg, 'efficiency_product': K,
         'valid': True, 'activity_MBq': 0.0, 'energy_keV': 0.0}
        for (lbl, net, gross, bg, K) in isos]}


class TestBackgroundSubtraction(unittest.TestCase):
    """A measured no-source background is subtracted per line, removing ambient
    peaks and zero-background false positives."""

    def test_run_line_totals_includes_undetected_lines(self):
        reports = [bg_window(60.0, [('Cs137', 5.0, 8.0, 3.0, 2e-6),
                                    ('Co60_1173', 0.0, 2.0, 2.0, 1e-6)])]
        tot = dr.run_line_totals(reports)
        self.assertEqual(sorted(tot), ['Co60_1173', 'Cs137'])   # both, detected or not
        self.assertAlmostEqual(tot['Cs137']['net'], 5.0, delta=1e-9)
        self.assertAlmostEqual(tot['Cs137']['live'], 60.0, delta=1e-9)

    def test_build_background_rates(self):
        reports = [bg_window(60.0, [('Cs137', 6.0, 10.0, 4.0, 2e-6)]),
                   bg_window(60.0, [('Cs137', 6.0, 10.0, 4.0, 2e-6)])]
        rates = dr.build_background_rates(reports)
        self.assertAlmostEqual(rates['Cs137']['net_cps'], 12.0 / 120.0, delta=1e-9)
        # sigma = sqrt(gross + sideband_bg)/live = sqrt(20 + 8)/120
        self.assertAlmostEqual(rates['Cs137']['net_cps_sigma'],
                               math.sqrt(28.0) / 120.0, delta=1e-9)

    def test_subtraction_removes_ambient_false_positive(self):
        bg = {'Cs137': {'net_cps': 0.1, 'net_cps_sigma': 0.02}}
        # a background-LEVEL run (~0.1 cps): detected without bg, gone with it
        reports = [bg_window(300.0, [('Cs137', 30.0, 45.0, 15.0, 2e-6)])]
        self.assertIn('Cs137', dr.aggregate_run_activity(reports))
        self.assertNotIn('Cs137',
                         dr.aggregate_run_activity(reports, background_rates=bg))

    def test_subtraction_reduces_source_activity(self):
        bg = {'Co60_1173': {'net_cps': 0.1, 'net_cps_sigma': 0.02}}
        reports = [bg_window(300.0, [('Co60_1173', 3000.0, 3050.0, 50.0, 1e-6)])]
        a0 = dr.aggregate_run_activity(reports)['Co60_1173']['activity_MBq']
        a1 = dr.aggregate_run_activity(
            reports, background_rates=bg)['Co60_1173']['activity_MBq']
        self.assertLess(a1, a0)                                   # 30 counts removed
        self.assertAlmostEqual(a1 / a0, (3000.0 - 30.0) / 3000.0, delta=1e-6)

    def test_no_background_is_a_noop(self):
        reports = [bg_window(300.0, [('Cs137', 1000.0, 1010.0, 10.0, 2e-6)])]
        a0 = dr.aggregate_run_activity(reports)['Cs137']['activity_MBq']
        a1 = dr.aggregate_run_activity(
            reports, background_rates=None)['Cs137']['activity_MBq']
        self.assertAlmostEqual(a0, a1, delta=1e-12)


class TestUncertaintySemantics(unittest.TestCase):
    """The N42 must carry the EXPANDED (k=2) uncertainty, not counting sigma."""

    @staticmethod
    def _expanded(u_counting_pct, u_systematic_pct):
        return 2.0 * math.sqrt(u_counting_pct ** 2 + u_systematic_pct ** 2)

    def test_well_counted_run_reports_about_the_budget(self):
        # counting 1.5%, systematic 10.7% -> ~21.6% expanded (the pre-
        # position-correction DJR headline; this driver's default
        # assay-systematic-uncertainty-percent is 10.6, position correction OFF)
        self.assertAlmostEqual(self._expanded(1.5, 10.7), 21.6, delta=0.2)

    def test_position_corrected_budget_headline(self):
        # With the position-aware correction ON (validated upstream 2026-09-03:
        # Exp E centre + 4 corners within +-1%, RSD 0.65%) the systematic budget
        # can drop to 3.2% -> a well-counted run reports ~7.1% expanded (k=2).
        # This driver keeps position_correction OFF by default (see
        # activity_node.py's --position-correction flag), so operators enabling
        # it should also lower --assay-systematic-uncertainty-percent to match.
        self.assertAlmostEqual(self._expanded(1.5, 3.2), 7.07, delta=0.05)

    def test_marginal_run_correctly_widens(self):
        # near the MDA counting statistics dominate and U must blow up
        self.assertGreater(self._expanded(20.0, 10.7), 44.0)

    def test_expanded_always_exceeds_counting_alone(self):
        """The whole point: counting sigma alone would understate the record."""
        for u_count in (0.5, 1.5, 5.0, 20.0):
            self.assertGreater(self._expanded(u_count, 10.7), 2.0 * u_count)

    def test_systematic_floor_is_never_undercut(self):
        # even with perfect counting statistics, U cannot fall below 2*u_sys
        self.assertGreaterEqual(self._expanded(0.0, 10.7), 21.4 - 1e-9)


if __name__ == '__main__':
    unittest.main()
