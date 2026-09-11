#!/usr/bin/env python
"""Unit tests for the GeGi activity physics.

These lock down the maths the Design Justification Report depends on: the
solid-angle geometry, the calibration-factor -> intrinsic-efficiency derivation,
the shielding attenuation, and the plate-derived standoff. A change that breaks
any of these silently invalidates the reported activities, so they are asserted
against independently-computed values (not re-derived from the code under test).

Run:  python -m unittest discover -s test    (with the ROS workspace sourced)
"""
from __future__ import print_function

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))

import activity_node as an  # noqa: E402


R = 0.045          # GeGi crystal radius (m)
CAL_D = 0.5        # calibration distance (m)
PLATE_T = 0.005    # steel plate thickness (m)


class TestSolidAngle(unittest.TestCase):
    """Omega/4pi = 0.5 * (1 - d/sqrt(d^2 + r^2))."""

    def test_calibration_geometry(self):
        # 0.5*(1 - 0.5/sqrt(0.25 + 0.002025))
        self.assertAlmostEqual(
            an.gegi_solid_angle_fraction(CAL_D, R), 0.0020127806, delta=1e-9)

    def test_operational_geometry(self):
        # 0.33 m: the standoff through the permanent plate
        self.assertAlmostEqual(
            an.gegi_solid_angle_fraction(0.33, R), 0.0045849160, delta=1e-9)

    def test_non_positive_distance_is_zero(self):
        self.assertEqual(an.gegi_solid_angle_fraction(0.0, R), 0.0)
        self.assertEqual(an.gegi_solid_angle_fraction(-0.2, R), 0.0)

    def test_monotonically_decreasing_with_distance(self):
        vals = [an.gegi_solid_angle_fraction(d, R)
                for d in (0.1, 0.2, 0.33, 0.5, 1.0)]
        for near, far in zip(vals, vals[1:]):
            self.assertGreater(near, far)

    def test_far_field_follows_inverse_square(self):
        # d >> r: Omega ~ 1/d^2, so halving distance quadruples solid angle
        ratio = (an.gegi_solid_angle_fraction(1.0, R)
                 / an.gegi_solid_angle_fraction(2.0, R))
        self.assertAlmostEqual(ratio, 4.0, delta=0.02)


class TestShielding(unittest.TestCase):
    """transmission = exp(-mu * n_plates * thickness)."""

    def test_one_steel_plate_per_line(self):
        # exp(-mu * 0.005) for the three calibrated gamma lines
        self.assertAlmostEqual(
            an.shield_transmission(57.4, 1, PLATE_T), 0.7505117288, delta=1e-9)  # Cs 662
        self.assertAlmostEqual(
            an.shield_transmission(41.8, 1, PLATE_T), 0.8113952356, delta=1e-9)  # Co 1173
        self.assertAlmostEqual(
            an.shield_transmission(39.6, 1, PLATE_T), 0.8203698531, delta=1e-9)  # Co 1332

    def test_plates_attenuate_multiplicatively(self):
        one = an.shield_transmission(41.8, 1, PLATE_T)
        two = an.shield_transmission(41.8, 2, PLATE_T)
        three = an.shield_transmission(41.8, 3, PLATE_T)
        self.assertAlmostEqual(two, one ** 2, delta=1e-9)
        self.assertAlmostEqual(three, one ** 3, delta=1e-9)

    def test_no_plates_means_no_attenuation(self):
        self.assertEqual(an.shield_transmission(41.8, 0, PLATE_T), 1.0)

    def test_no_mu_means_no_attenuation(self):
        self.assertEqual(an.shield_transmission(0.0, 3, PLATE_T), 1.0)

    def test_transmission_is_bounded(self):
        for n in range(0, 6):
            t = an.shield_transmission(57.4, n, PLATE_T)
            self.assertGreater(t, 0.0)
            self.assertLessEqual(t, 1.0)

    # --- material-agnostic invariants: these hold for steel, lead, anything ---

    def test_more_attenuating_material_transmits_less(self):
        steel, lead_like, tungsten_like = 41.8, 120.0, 250.0
        t_steel = an.shield_transmission(steel, 1, PLATE_T)
        t_lead = an.shield_transmission(lead_like, 1, PLATE_T)
        t_w = an.shield_transmission(tungsten_like, 1, PLATE_T)
        self.assertGreater(t_steel, t_lead)
        self.assertGreater(t_lead, t_w)

    def test_thicker_plates_transmit_less(self):
        for mu in (41.8, 120.0):
            thin = an.shield_transmission(mu, 1, 0.003)
            thick = an.shield_transmission(mu, 1, 0.010)
            self.assertGreater(thin, thick)

    def test_more_plates_transmit_less(self):
        for mu in (41.8, 120.0):
            prev = 1.0
            for n in range(1, 5):
                t = an.shield_transmission(mu, n, PLATE_T)
                self.assertLess(t, prev)
                prev = t

    def test_beer_lambert_holds_for_any_material_and_thickness(self):
        """transmission == exp(-mu * n * t) for arbitrary inputs."""
        for mu in (10.0, 41.8, 120.0, 250.0):
            for thickness in (0.002, 0.005, 0.012):
                for n in (1, 2, 5):
                    self.assertAlmostEqual(
                        an.shield_transmission(mu, n, thickness),
                        math.exp(-mu * n * thickness), delta=1e-12)

    # --- measured per-plate-count transmission table (broad-beam build-up) ---

    TABLE = {1: 0.8141, 2: 0.7178}   # Co-60 1173 keV, measured 2026-09-04

    def test_tabulated_plate_count_uses_exact_measured_value(self):
        self.assertEqual(
            an.shield_transmission(34.8, 1, PLATE_T, self.TABLE), 0.8141)
        self.assertEqual(
            an.shield_transmission(34.8, 2, PLATE_T, self.TABLE), 0.7178)

    def test_zero_plates_is_unity_even_with_table(self):
        self.assertEqual(
            an.shield_transmission(34.8, 0, PLATE_T, self.TABLE), 1.0)

    def test_beyond_table_extrapolates_from_highest_tabulated(self):
        # 3 plates = measured T(2) * one exponential plate
        expected = 0.7178 * math.exp(-34.8 * PLATE_T)
        self.assertAlmostEqual(
            an.shield_transmission(34.8, 3, PLATE_T, self.TABLE),
            expected, delta=1e-12)

    def test_gap_below_table_extrapolates_from_lower_entry(self):
        # table has only n=1: n=2 = T(1) * one exponential plate
        expected = 0.8141 * math.exp(-34.8 * PLATE_T)
        self.assertAlmostEqual(
            an.shield_transmission(34.8, 2, PLATE_T, {1: 0.8141}),
            expected, delta=1e-12)

    def test_empty_or_none_table_falls_back_to_exponential(self):
        for table in (None, {}):
            self.assertAlmostEqual(
                an.shield_transmission(41.8, 2, PLATE_T, table),
                math.exp(-41.8 * 2 * PLATE_T), delta=1e-12)

    def test_string_keys_from_yaml_are_accepted(self):
        self.assertEqual(
            an.shield_transmission(34.8, 2, PLATE_T, {'2': 0.7178}), 0.7178)


class TestPlateDerivedDistance(unittest.TestCase):
    """distance = base_standoff + total_plates * thickness."""

    def test_operational_geometry_is_330mm(self):
        # bare 0.325 m + the one permanent plate = the measured 0.33 m standoff
        self.assertAlmostEqual(
            an.plate_derived_distance(0.325, 1, PLATE_T), 0.330, delta=1e-9)

    def test_each_plate_adds_its_thickness(self):
        self.assertAlmostEqual(
            an.plate_derived_distance(0.325, 3, PLATE_T), 0.340, delta=1e-9)

    def test_zero_plates_is_bare_standoff(self):
        self.assertAlmostEqual(
            an.plate_derived_distance(0.325, 0, PLATE_T), 0.325, delta=1e-9)


def _cs137_cfg(calibration_factor=45672.0, **overrides):
    cfg = {
        'energy_keV': 661.7,
        'emission_probability': 0.851,
        'peak_roi_keV': [655.0, 669.0],
        'left_sideband_keV': [620.0, 645.0],
        'right_sideband_keV': [680.0, 705.0],
        'calibration_factor': calibration_factor,
        'mu_shield_per_m': 57.4,
    }
    cfg.update(overrides)
    return cfg


class TestIsotopeConfig(unittest.TestCase):
    """The calibration-factor -> intrinsic-efficiency chain."""

    def setUp(self):
        self.bin_edges = np.linspace(0.0, 3000.0, 1024)

    def _make(self, cfg):
        return an.IsotopeConfig('Cs137', cfg, self.bin_edges,
                                calibration_distance_m=CAL_D, crystal_radius_m=R)

    def test_intrinsic_efficiency_derived_from_calibration_factor(self):
        ic = self._make(_cs137_cfg())
        # eps = 1 / (CF * Omega_cal * I_gamma) = 1/(45672 * 0.0020127806 * 0.851)
        self.assertAlmostEqual(ic.intrinsic_efficiency, 0.0127827406, delta=1e-9)

    def test_calibration_factor_round_trip(self):
        """THE contract: at the calibration distance, activity = CF * net_cps.

        This is what the calibration factor *means*. It exercises the whole
        efficiency chain, so any change to the solid-angle formula or the
        efficiency derivation breaks it.
        """
        cf = 45672.0
        ic = self._make(_cs137_cfg(calibration_factor=cf))
        net_counts, live_s = 1000.0, 100.0
        omega_cal = an.gegi_solid_angle_fraction(CAL_D, R)
        activity_bq = net_counts / (ic.intrinsic_efficiency * omega_cal
                                    * ic.emission_probability * live_s)
        expected = cf * (net_counts / live_s)          # 45672 * 10 cps
        self.assertAlmostEqual(activity_bq, expected, delta=expected * 1e-9)

    def test_measured_efficiency_overrides_calibration_factor(self):
        ic = self._make(_cs137_cfg(measured_intrinsic_efficiency=0.05))
        self.assertAlmostEqual(ic.intrinsic_efficiency, 0.05, delta=1e-12)

    def test_falls_back_to_polynomial_without_calibration_factor(self):
        ic = self._make(_cs137_cfg(calibration_factor=0.0))
        expected = an.gegi_intrinsic_efficiency(661.7)
        self.assertAlmostEqual(ic.intrinsic_efficiency, expected, delta=1e-12)
        self.assertGreater(ic.intrinsic_efficiency, 0.0)

    def test_efficiency_is_physically_plausible(self):
        ic = self._make(_cs137_cfg())
        self.assertGreater(ic.intrinsic_efficiency, 0.0)
        self.assertLess(ic.intrinsic_efficiency, 1.0)

    def test_mu_defaults_to_zero_when_absent(self):
        cfg = _cs137_cfg()
        del cfg['mu_shield_per_m']
        self.assertEqual(self._make(cfg).mu_shield_per_m, 0.0)

    def test_roi_maps_to_contiguous_channels_covering_the_peak(self):
        ic = self._make(_cs137_cfg())
        ch = ic.peak_channels
        self.assertGreater(len(ch), 0)
        self.assertTrue(np.all(np.diff(ch) == 1), "channels must be contiguous")
        # the ROI must actually bracket the 662 keV line
        self.assertLessEqual(self.bin_edges[ch[0]], 661.7)
        self.assertGreaterEqual(self.bin_edges[ch[-1]] + (
            self.bin_edges[1] - self.bin_edges[0]), 661.7)

    def test_sidebands_sit_outside_the_peak(self):
        ic = self._make(_cs137_cfg())
        self.assertLess(ic.left_channels[-1], ic.peak_channels[0])
        self.assertGreater(ic.right_channels[0], ic.peak_channels[-1])


class TestOffAxisSolidAngle(unittest.TestCase):
    """Position-aware correction: ratio = (d0/d')^exponent.

    d' = sqrt(d0^2 + rho^2) is the slant distance to a source displaced rho
    laterally. Default exponent 2.0 = inverse-square only, the MEASURED model
    for the GeGI (corner proof 2026-09-03: publishing the slant distance
    recovered the Co-60 cert to +1.6% - flat-disk foreshortening is cancelled
    by the oblique 11-mm crystal chord). exponent 3.0 = naive flat-disk.
    """

    D0 = 0.580     # operational standoff (m)
    RHO = 0.250    # tray-corner lateral offset (m)

    def test_on_axis_is_unity(self):
        self.assertEqual(an.off_axis_solid_angle_ratio(self.D0, 0.0), 1.0)

    def test_degenerate_inputs_are_unity(self):
        self.assertEqual(an.off_axis_solid_angle_ratio(0.0, self.RHO), 1.0)
        self.assertEqual(an.off_axis_solid_angle_ratio(-1.0, self.RHO), 1.0)
        self.assertEqual(an.off_axis_solid_angle_ratio(self.D0, -0.1), 1.0)

    def test_default_is_inverse_square(self):
        # d' = sqrt(0.58^2 + 0.25^2) = 0.6315853 m -> (0.58/0.6315853)^2
        d_slant = math.sqrt(self.D0 ** 2 + self.RHO ** 2)
        expected = (self.D0 / d_slant) ** 2
        self.assertAlmostEqual(
            an.off_axis_solid_angle_ratio(self.D0, self.RHO), expected,
            delta=1e-12)
        # ~ -16% inverse-square deficit at the corner
        self.assertAlmostEqual(expected, 0.8433, delta=5e-4)

    def test_default_equals_solid_angle_ratio_far_field(self):
        """exponent 2 == the actual Omega(d')/Omega(d0) ratio (far field)."""
        d_slant = math.sqrt(self.D0 ** 2 + self.RHO ** 2)
        omega_ratio = (an.gegi_solid_angle_fraction(d_slant, R)
                       / an.gegi_solid_angle_fraction(self.D0, R))
        self.assertAlmostEqual(
            an.off_axis_solid_angle_ratio(self.D0, self.RHO), omega_ratio,
            delta=2e-3)

    def test_flat_disk_exponent_adds_foreshortening(self):
        d_slant = math.sqrt(self.D0 ** 2 + self.RHO ** 2)
        cos_theta = self.D0 / d_slant
        self.assertAlmostEqual(
            an.off_axis_solid_angle_ratio(self.D0, self.RHO, exponent=3.0),
            cos_theta * (self.D0 / d_slant) ** 2, delta=1e-12)
        # naive flat-disk predicts ~ -23% at the corner
        self.assertAlmostEqual(
            an.off_axis_solid_angle_ratio(self.D0, self.RHO, exponent=3.0),
            0.7745, delta=5e-4)

    def test_monotonically_decreasing_with_offset(self):
        for exponent in (2.0, 3.0):
            vals = [an.off_axis_solid_angle_ratio(self.D0, rho, exponent)
                    for rho in (0.05, 0.10, 0.20, 0.30, 0.50)]
            for near, far in zip(vals, vals[1:]):
                self.assertGreater(near, far)
            for v in vals:
                self.assertGreater(v, 0.0)
                self.assertLess(v, 1.0)


class TestSlantShieldFactor(unittest.TestCase):
    """Extra transmission for the slant path through the plates:
    exp(-mu * n * t * (1/cos(theta) - 1)); the on-axis part is applied
    separately by shield_transmission."""

    D0 = 0.580
    RHO = 0.250

    def test_on_axis_is_unity(self):
        self.assertEqual(
            an.slant_shield_factor(41.8, 2, PLATE_T, self.D0, 0.0), 1.0)

    def test_no_plates_is_unity(self):
        self.assertEqual(
            an.slant_shield_factor(41.8, 0, PLATE_T, self.D0, self.RHO), 1.0)

    def test_no_mu_is_unity(self):
        self.assertEqual(
            an.slant_shield_factor(0.0, 2, PLATE_T, self.D0, self.RHO), 1.0)

    def test_corner_value_one_plate_1173(self):
        cos_theta = self.D0 / math.sqrt(self.D0 ** 2 + self.RHO ** 2)
        expected = math.exp(-41.8 * 1 * PLATE_T * (1.0 / cos_theta - 1.0))
        got = an.slant_shield_factor(41.8, 1, PLATE_T, self.D0, self.RHO)
        self.assertAlmostEqual(got, expected, delta=1e-12)
        # small effect: ~ -1.9% for one extra plate at the corner
        self.assertGreater(got, 0.97)
        self.assertLess(got, 1.0)

    def test_total_slant_transmission_equals_full_slant_path(self):
        """on-axis transmission x slant factor == exp(-mu * n * t / cos)."""
        cos_theta = self.D0 / math.sqrt(self.D0 ** 2 + self.RHO ** 2)
        for n in (1, 2, 3):
            combined = (an.shield_transmission(41.8, n, PLATE_T)
                        * an.slant_shield_factor(41.8, n, PLATE_T,
                                                 self.D0, self.RHO))
            self.assertAlmostEqual(
                combined, math.exp(-41.8 * n * PLATE_T / cos_theta),
                delta=1e-12)


class TestNuclideNameMatching(unittest.TestCase):
    """Assay isotope names must match the imaging hotspot labels."""

    def test_assay_lines_match_hotspot_labels(self):
        self.assertEqual(an.normalize_nuclide_name('Cs137'),
                         an.normalize_nuclide_name('Cs-137'))
        self.assertEqual(an.normalize_nuclide_name('Co60_1173'),
                         an.normalize_nuclide_name('Co-60'))
        self.assertEqual(an.normalize_nuclide_name('Co60_1332'),
                         an.normalize_nuclide_name('Co-60'))

    def test_both_co60_lines_share_one_key(self):
        self.assertEqual(an.normalize_nuclide_name('Co60_1173'),
                         an.normalize_nuclide_name('Co60_1332'))

    def test_distinct_nuclides_stay_distinct(self):
        keys = {an.normalize_nuclide_name(n)
                for n in ('Cs-137', 'Co-60', 'Eu-152', 'Am-241')}
        self.assertEqual(len(keys), 4)


class TestParseHotspots(unittest.TestCase):
    """'/source_isotopes' + '/source_directions' pairing."""

    def test_typical_two_source_frame(self):
        hs = an.parse_hotspots('Cs-137:142|Co-60:98',
                               [(0.10, -0.05), (0.0, 0.20)])
        self.assertEqual(set(hs.keys()), {'cs137', 'co60'})
        self.assertAlmostEqual(hs['cs137']['offset_m'],
                               math.sqrt(0.10 ** 2 + 0.05 ** 2), delta=1e-12)
        self.assertAlmostEqual(hs['co60']['offset_m'], 0.20, delta=1e-12)
        self.assertEqual(hs['cs137']['count'], 142)

    def test_none_and_empty_yield_no_hotspots(self):
        self.assertEqual(an.parse_hotspots('none', []), {})
        self.assertEqual(an.parse_hotspots('', []), {})

    def test_duplicate_nuclide_keeps_dominant_hotspot(self):
        # same nuclide imaged at two positions: the higher-count one wins
        hs = an.parse_hotspots('Co-60:40|Co-60:90',
                               [(0.30, 0.0), (0.05, 0.0)])
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs['co60']['count'], 90)
        self.assertAlmostEqual(hs['co60']['offset_m'], 0.05, delta=1e-12)

    def test_assay_line_lookup_finds_its_hotspot(self):
        hs = an.parse_hotspots('Co-60:98', [(0.15, -0.20)])
        self.assertIn(an.normalize_nuclide_name('Co60_1332'), hs)


class TestAverageHotspot(unittest.TestCase):
    """Window-averaged hotspot position (damps per-frame imaging jitter)."""

    def test_empty_returns_none(self):
        self.assertIsNone(an.average_hotspot([]))

    def test_single_sample_passes_through(self):
        avg = an.average_hotspot([{'y_m': 0.18, 'z_m': 0.20}])
        self.assertAlmostEqual(avg['y_m'], 0.18, delta=1e-12)
        self.assertAlmostEqual(avg['z_m'], 0.20, delta=1e-12)
        self.assertAlmostEqual(avg['offset_m'],
                               math.sqrt(0.18 ** 2 + 0.20 ** 2), delta=1e-12)
        self.assertEqual(avg['n_samples'], 1)

    def test_vector_mean_of_jittered_samples(self):
        # symmetric jitter around (0.17, 0.17) must average back to it
        samples = [{'y_m': 0.17 + dy, 'z_m': 0.17 + dz}
                   for (dy, dz) in ((0.03, 0.0), (-0.03, 0.0),
                                    (0.0, 0.03), (0.0, -0.03))]
        avg = an.average_hotspot(samples)
        self.assertAlmostEqual(avg['y_m'], 0.17, delta=1e-12)
        self.assertAlmostEqual(avg['z_m'], 0.17, delta=1e-12)
        self.assertAlmostEqual(avg['offset_m'], 0.17 * math.sqrt(2.0),
                               delta=1e-12)
        self.assertEqual(avg['n_samples'], 4)

    def test_vector_mean_beats_scalar_mean_of_offsets(self):
        """Averaging |offset| carries a positive noise bias the vector mean
        does not: for symmetric jitter, mean(|r_i|) > |mean(r_i)|."""
        samples = [{'y_m': 0.17 + dy, 'z_m': 0.17 + dz}
                   for (dy, dz) in ((0.05, 0.0), (-0.05, 0.0),
                                    (0.0, 0.05), (0.0, -0.05))]
        scalar_mean = sum(math.sqrt(s['y_m'] ** 2 + s['z_m'] ** 2)
                          for s in samples) / 4.0
        self.assertLess(an.average_hotspot(samples)['offset_m'], scalar_mean)


class TestIntrinsicEfficiencyPolynomial(unittest.TestCase):
    def test_zero_or_negative_energy_is_zero(self):
        self.assertEqual(an.gegi_intrinsic_efficiency(0.0), 0.0)
        self.assertEqual(an.gegi_intrinsic_efficiency(-100.0), 0.0)

    def test_positive_and_sub_unity_over_the_working_range(self):
        for e in (200.0, 662.0, 1173.0, 1332.0):
            eps = an.gegi_intrinsic_efficiency(e)
            self.assertGreater(eps, 0.0)
            self.assertLess(eps, 1.0)


if __name__ == '__main__':
    unittest.main()
