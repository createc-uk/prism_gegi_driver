# -*- coding: utf-8 -*-
"""Unit tests for the isotope identification (screening) layer.

Pure logic on synthetic spectra - no detector, no roscore. The synthetic
spectra deliberately reproduce the failure modes that motivated this layer:
a weak peak riding on a dominant nuclide's Compton continuum, and peaks the
fixed-ROI assay layer cannot see at all.
"""
from __future__ import print_function

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))

import isotope_id  # noqa: E402

LIB_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'config', 'nuclide_library.yaml'))


def make_energies(lo=30.0, hi=3000.0, bin_kev=1.5):
    return np.arange(lo, hi, bin_kev) + bin_kev / 2.0


def add_peak(counts, energies, energy_kev, area):
    """Add a Gaussian photopeak with the library's model FWHM."""
    sigma = isotope_id.fwhm_kev(energy_kev) / 2.3548
    counts += area * np.exp(-0.5 * ((energies - energy_kev) / sigma) ** 2) \
        / (sigma * math.sqrt(2.0 * math.pi)) * (energies[1] - energies[0])
    return counts


def add_compton_continuum(counts, energies, edge_kev, level):
    """Flat-ish continuum up to a Compton edge with a smooth rolloff."""
    counts += level / (1.0 + np.exp((energies - edge_kev) / 15.0))
    return counts


class TestPeakSearch(unittest.TestCase):
    def test_lone_peak_found(self):
        e = make_energies()
        y = np.full(len(e), 50.0)
        add_peak(y, e, 661.66, 5000.0)
        peaks = isotope_id.find_peaks(y, e)
        self.assertEqual(len(peaks), 1)
        self.assertAlmostEqual(peaks[0]['energy_keV'], 661.66, delta=1.0)
        self.assertGreater(peaks[0]['snr'], 10.0)

    def test_flat_spectrum_has_no_peaks(self):
        e = make_energies()
        y = np.full(len(e), 200.0)
        self.assertEqual(isotope_id.find_peaks(y, e), [])

    def test_smooth_continuum_alone_is_not_a_peak(self):
        # The kernel nulls smooth continuum - a Compton shelf must not fire.
        e = make_energies()
        y = np.full(len(e), 20.0)
        add_compton_continuum(y, e, 477.0, 3000.0)
        peaks = isotope_id.find_peaks(y, e)
        # allow nothing anywhere near the shelf plateau; the edge rolloff is
        # 15-keV smooth so it must not look like a detector-width peak either
        self.assertEqual(len(peaks), 0)

    def test_weak_peak_on_dominant_continuum(self):
        # THE motivating failure mode: weak Cs-137 on a huge Co-60 continuum.
        e = make_energies()
        y = np.full(len(e), 30.0)
        add_compton_continuum(y, e, 963.0, 20000.0)   # Co-60 1173 edge
        add_compton_continuum(y, e, 1118.0, 15000.0)  # Co-60 1332 edge
        add_peak(y, e, 1173.23, 400000.0)
        add_peak(y, e, 1332.49, 360000.0)
        add_peak(y, e, 661.66, 8000.0)   # weak Cs on ~35k/bin continuum
        peaks = isotope_id.find_peaks(y, e)
        found = [p['energy_keV'] for p in peaks]
        self.assertTrue(any(abs(f - 661.66) < 1.5 for f in found),
                        "weak 662 not found in %s" % found)
        self.assertTrue(any(abs(f - 1173.23) < 1.5 for f in found))
        self.assertTrue(any(abs(f - 1332.49) < 1.5 for f in found))


class TestLibrary(unittest.TestCase):
    def test_library_loads_and_is_sane(self):
        lib = isotope_id.load_nuclide_library(LIB_PATH)
        self.assertGreaterEqual(len(lib['nuclides']), 25)
        for name, nuc in lib['nuclides'].items():
            self.assertIn(nuc.get('category'), ('SNM', 'IND', 'NORM'),
                          "%s bad category" % name)
            for line in nuc['lines']:
                self.assertGreater(line['energy_keV'], 30.0)
                self.assertLess(line['energy_keV'], 3000.0)
                self.assertGreater(line['yield'], 0.0)
                self.assertLessEqual(line['yield'], 2.0)  # 511 annihilation max

    def test_expected_nuclides_present(self):
        lib = isotope_id.load_nuclide_library(LIB_PATH)
        for name in ('Am-241', 'U-235', 'U-238', 'Pu-239', 'Co-60', 'Cs-137',
                     'K-40', 'Ra-226', 'Tl-208'):
            self.assertIn(name, lib['nuclides'])


class TestTransmission(unittest.TestCase):
    def test_consistent_with_assay_config_mu(self):
        # isotopes.yaml uses 57.4/m at 662 keV -> T(5mm) = exp(-0.287)
        t = isotope_id.steel_transmission(662.0, 0.005)
        self.assertAlmostEqual(t, math.exp(-57.4 * 0.005), delta=0.02)

    def test_low_energy_is_plate_blind(self):
        # Am-241 59.5 keV through 5 mm steel: ~1% - must carry ~no weight.
        self.assertLess(isotope_id.steel_transmission(59.54, 0.005), 0.02)

    def test_no_plate_is_unity(self):
        self.assertEqual(isotope_id.steel_transmission(59.54, 0.0), 1.0)


class TestMatching(unittest.TestCase):
    def _identify(self, spec_builder, **kw):
        e = make_energies()
        y = np.full(len(e), 40.0)
        spec_builder(y, e)
        lib = isotope_id.load_nuclide_library(LIB_PATH)
        return isotope_id.identify(y, e, lib, **kw)

    def test_cs137_identified(self):
        _, results, _ = self._identify(
            lambda y, e: add_peak(y, e, 661.66, 5000.0))
        ids = [r['nuclide'] for r in results if r['identified']]
        self.assertEqual(ids, ['Cs-137'])

    def test_co60_and_weak_cs137_both_identified(self):
        # Regression for the real 2026-08 incident: Cs-137 under Co-60.
        def build(y, e):
            add_compton_continuum(y, e, 963.0, 20000.0)
            add_peak(y, e, 1173.23, 400000.0)
            add_peak(y, e, 1332.49, 360000.0)
            add_peak(y, e, 661.66, 8000.0)
        _, results, unknown = self._identify(build)
        ids = sorted(r['nuclide'] for r in results if r['identified'])
        self.assertEqual(ids, ['Co-60', 'Cs-137'])
        self.assertEqual(unknown, [])

    def test_k40_identified(self):
        _, results, _ = self._identify(
            lambda y, e: add_peak(y, e, 1460.82, 3000.0))
        ids = [r['nuclide'] for r in results if r['identified']]
        self.assertEqual(ids, ['K-40'])

    def test_u235_ra226_ambiguity_flagged(self):
        # One ~186 keV peak: both nuclides match it; the flag must say so.
        _, results, _ = self._identify(
            lambda y, e: add_peak(y, e, 185.9, 8000.0), min_score=0.2)
        by_name = {r['nuclide']: r for r in results}
        u, ra = by_name['U-235'], by_name['Ra-226']
        self.assertTrue(u['matched_lines'] and ra['matched_lines'])
        self.assertTrue(any('Ra-226' in s['also'] for s in u['shared_peaks']))
        self.assertTrue(any('U-235' in s['also'] for s in ra['shared_peaks']))

    def test_u235_needs_companions_ra226_does_not(self):
        # A lone 186 peak fully satisfies Ra-226 (its only line) but only
        # partially satisfies U-235 (143.8/205.3 companions missing).
        _, results, _ = self._identify(
            lambda y, e: add_peak(y, e, 185.9, 8000.0), min_score=0.2)
        by_name = {r['nuclide']: r for r in results}
        self.assertGreater(by_name['Ra-226']['score'],
                           by_name['U-235']['score'])

    def test_eu152_identified_without_representative_line(self):
        # Regression for the 2026-08-14 live run: Eu-152 blazing at
        # 122/245/344/779 keV but its 1408 keV representative below detection.
        # The multi-line evidence rule must identify it anyway.
        def build(y, e):
            add_peak(y, e, 121.78, 60000.0)
            add_peak(y, e, 244.70, 8000.0)
            add_peak(y, e, 344.28, 20000.0)
            add_peak(y, e, 778.90, 6000.0)
        _, results, _ = self._identify(build)
        by_name = {r['nuclide']: r for r in results}
        self.assertTrue(by_name['Eu-152']['identified'],
                        "score=%.2f" % by_name['Eu-152']['score'])

    def test_co57_not_false_identified_from_eu152_line(self):
        # Same run: Eu-152's 121.8 keV peak sits 0.28 keV from Co-57's
        # 122.06 keV line. With Eu-152 identified (multi-line), Co-57's only
        # evidence is a shared peak -> demoted, not identified.
        def build(y, e):
            add_peak(y, e, 121.78, 60000.0)
            add_peak(y, e, 244.70, 8000.0)
            add_peak(y, e, 344.28, 20000.0)
            add_peak(y, e, 778.90, 6000.0)
        _, results, _ = self._identify(build)
        by_name = {r['nuclide']: r for r in results}
        self.assertFalse(by_name['Co-57']['identified'])
        self.assertIn('Eu-152', by_name['Co-57'].get('subsumed_by', []))

    def test_real_co57_still_identified_alone(self):
        # A genuine Co-57 source (122 + 136.5, nothing else) must still ID.
        def build(y, e):
            add_peak(y, e, 122.06, 20000.0)
            add_peak(y, e, 136.47, 3000.0)
        _, results, _ = self._identify(build)
        ids = [r['nuclide'] for r in results if r['identified']]
        self.assertIn('Co-57', ids)

    def test_symmetric_ambiguity_demotes_neither(self):
        # U-235 vs Ra-226 sharing one 186 keV peak: neither has independent
        # lines, so BOTH stay (flagged), per the demotion rule.
        _, results, _ = self._identify(
            lambda y, e: add_peak(y, e, 186.1, 8000.0), min_score=0.03)
        by_name = {r['nuclide']: r for r in results}
        self.assertNotIn('subsumed_by', by_name['Ra-226'])
        self.assertNotIn('subsumed_by', by_name['U-235'])

    def test_weighted_scoring_survives_missing_weak_line(self):
        # Eu-152 with only its 5 strongest lines visible must still identify
        # (the paper's strict AND logic would reject it).
        def build(y, e):
            for E in (121.78, 344.28, 778.90, 964.06, 1408.01):
                add_peak(y, e, E, 20000.0)
        _, results, _ = self._identify(build)
        ids = [r['nuclide'] for r in results if r['identified']]
        self.assertIn('Eu-152', ids)

    def test_plate_blind_line_not_counted_against_nuclide(self):
        # U-238 seen ONLY via 1001 keV (Pa-234m): the plate-blinded 63/93 keV
        # Th-234 lines must not drag the score below threshold.
        _, results, _ = self._identify(
            lambda y, e: add_peak(y, e, 1001.03, 4000.0))
        by_name = {r['nuclide']: r for r in results}
        self.assertTrue(by_name['U-238']['identified'],
                        "score=%.2f" % by_name['U-238']['score'])

    def test_unknown_peak_reported(self):
        # A peak at an energy in no library nuclide must surface as unknown.
        _, results, unknown = self._identify(
            lambda y, e: add_peak(y, e, 1520.0, 5000.0))
        self.assertEqual([r for r in results if r['identified']], [])
        self.assertEqual(len(unknown), 1)
        self.assertAlmostEqual(unknown[0]['energy_keV'], 1520.0, delta=1.5)


class TestLineLabels(unittest.TestCase):
    """label_peaks / energy_drift_kev / mark_persistent (pure helpers behind
    the /identified_lines topic)."""

    PEAKS = [{'energy_keV': 661.7, 'snr': 40.0, 'channel': 0},
             {'energy_keV': 187.2, 'snr': 35.0, 'channel': 1},
             {'energy_keV': 511.2, 'snr': 8.0, 'channel': 2},
             {'energy_keV': 1520.0, 'snr': 6.0, 'channel': 3}]
    RESULTS = [{'nuclide': 'Cs-137', 'identified': True,
                'matched_lines': [{'energy_keV': 661.66, 'peak_keV': 661.7,
                                   'snr': 40.0}]},
               {'nuclide': 'U-235', 'identified': False, 'matched_lines': []}]

    def test_labels_matched_unknown_and_tags(self):
        lines = isotope_id.label_peaks(self.PEAKS, self.RESULTS)
        by_e = {l['energy_keV']: l for l in lines}
        self.assertEqual(by_e[661.7]['label'], 'Cs-137')
        self.assertEqual(by_e[1520.0]['label'], '?')
        self.assertIn('annihilation', by_e[511.2]['tags'])
        # 187 keV is backscatter-suspect BECAUSE a strong >400 keV peak exists
        self.assertIn('backscatter-suspect', by_e[187.2]['tags'])

    def test_no_backscatter_tag_without_strong_source(self):
        weak = [{'energy_keV': 187.2, 'snr': 10.0, 'channel': 0}]
        lines = isotope_id.label_peaks(weak, [])
        self.assertEqual(lines[0]['tags'], [])

    def test_shared_peak_label_joined(self):
        peaks = [{'energy_keV': 186.0, 'snr': 12.0, 'channel': 0}]
        results = [
            {'nuclide': 'U-235', 'identified': True,
             'matched_lines': [{'energy_keV': 185.72, 'peak_keV': 186.0,
                                'snr': 12.0}]},
            {'nuclide': 'Ra-226', 'identified': True,
             'matched_lines': [{'energy_keV': 186.21, 'peak_keV': 186.0,
                                'snr': 12.0}]}]
        lines = isotope_id.label_peaks(peaks, results)
        self.assertEqual(lines[0]['label'], 'U-235/Ra-226')

    def test_energy_drift_detects_offset(self):
        results = [{'nuclide': 'Cs-137', 'identified': True,
                    'matched_lines': [{'energy_keV': 661.66,
                                       'peak_keV': 662.9, 'snr': 40.0}]},
                   {'nuclide': 'Co-60', 'identified': True,
                    'matched_lines': [{'energy_keV': 1173.23,
                                       'peak_keV': 1174.4, 'snr': 20.0}]}]
        drift, n = isotope_id.energy_drift_kev(results)
        self.assertEqual(n, 2)
        self.assertGreater(drift, 1.0)   # ~+1.2 keV, SNR-weighted

    def test_energy_drift_empty(self):
        self.assertEqual(isotope_id.energy_drift_kev([]), (0.0, 0))

    def test_persistence_two_pass(self):
        lines1 = [{'energy_keV': 661.7}, {'energy_keV': 187.2}]
        prev = isotope_id.mark_persistent(lines1, [])
        self.assertFalse(any(l['persistent'] for l in lines1))  # first sight
        lines2 = [{'energy_keV': 661.8}, {'energy_keV': 900.0}]
        isotope_id.mark_persistent(lines2, prev)
        self.assertTrue(lines2[0]['persistent'])    # seen last pass (0.1 keV off)
        self.assertFalse(lines2[1]['persistent'])   # brand new

    def test_contested_label_gets_question_mark(self):
        # Weak-Eu regime (2026-08-14 safe-leakage screenshot): a lone 122 keV
        # peak identifies Co-57, but Eu-152 also fits -> label 'Co-57?'.
        peaks = [{'energy_keV': 121.9, 'snr': 20.0, 'channel': 0}]
        results = [
            {'nuclide': 'Co-57', 'identified': True,
             'matched_lines': [{'energy_keV': 122.06, 'peak_keV': 121.9,
                                'snr': 20.0}]},
            {'nuclide': 'Eu-152', 'identified': False,
             'matched_lines': [{'energy_keV': 121.78, 'peak_keV': 121.9,
                                'snr': 20.0}]}]
        lines = isotope_id.label_peaks(peaks, results)
        self.assertEqual(lines[0]['label'], 'Co-57?')
        self.assertIn('also-matches:Eu-152', lines[0]['tags'])

    def test_unknown_peak_with_candidate_hint(self):
        # A '?' peak that WOULD fit a library nuclide carries a hint tag.
        peaks = [{'energy_keV': 344.3, 'snr': 8.0, 'channel': 0}]
        results = [{'nuclide': 'Eu-152', 'identified': False,
                    'matched_lines': [{'energy_keV': 344.28, 'peak_keV': 344.3,
                                       'snr': 8.0}]}]
        lines = isotope_id.label_peaks(peaks, results)
        self.assertEqual(lines[0]['label'], '?')
        self.assertIn('candidates:Eu-152', lines[0]['tags'])

    def test_subsumed_nuclide_casts_no_doubt(self):
        # Strong-Eu regime: Co-57 was DEMOTED (subsumed) - the Eu-152 label
        # must stay clean, no '?' suffix.
        peaks = [{'energy_keV': 121.8, 'snr': 40.0, 'channel': 0}]
        results = [
            {'nuclide': 'Eu-152', 'identified': True,
             'matched_lines': [{'energy_keV': 121.78, 'peak_keV': 121.8,
                                'snr': 40.0}]},
            {'nuclide': 'Co-57', 'identified': False,
             'subsumed_by': ['Eu-152'],
             'matched_lines': [{'energy_keV': 122.06, 'peak_keV': 121.8,
                                'snr': 40.0}]}]
        lines = isotope_id.label_peaks(peaks, results)
        self.assertEqual(lines[0]['label'], 'Eu-152')

    def test_strong_peak_is_persistent_immediately(self):
        # A >=10-sigma peak is no statistical wiggle - labelled on FIRST sight;
        # a weak one still waits for confirmation.
        lines = [{'energy_keV': 661.7, 'snr': 40.0},
                 {'energy_keV': 900.0, 'snr': 5.0}]
        isotope_id.mark_persistent(lines, [])
        self.assertTrue(lines[0]['persistent'])
        self.assertFalse(lines[1]['persistent'])


class TestScreeningXml(unittest.TestCase):
    """N42 screening block (data_recorder.screening_xml_block, pure)."""

    def _results(self):
        return [
            {'nuclide': 'Eu-152', 'category': 'IND', 'score': 0.83,
             'identified': True, 'shared_peaks': [],
             'matched_lines': [{'energy_keV': 344.28, 'snr': 12.0},
                               {'energy_keV': 1408.01, 'snr': 9.0}]},
            {'nuclide': 'Cs-137', 'category': 'IND', 'score': 1.0,
             'identified': True, 'shared_peaks': [],
             'matched_lines': [{'energy_keV': 661.66, 'snr': 40.0}]},
            {'nuclide': 'U-235', 'category': 'SNM', 'score': 0.2,
             'identified': False, 'shared_peaks': [], 'matched_lines': []},
        ]

    def test_identified_non_assay_nuclide_emitted(self):
        import data_recorder_node as dr
        blocks, _ = dr.screening_xml_block(self._results(), [],
                                           exclude_names={'Cs-137', 'Co-60'})
        self.assertEqual(len(blocks), 1)
        self.assertIn('Eu-152', blocks[0])
        self.assertIn('SCREENING', blocks[0])
        self.assertIn('score 0.83', blocks[0])
        self.assertNotIn('Cs-137', blocks[0])

    def test_assay_nuclides_excluded_and_unidentified_skipped(self):
        import data_recorder_node as dr
        blocks, _ = dr.screening_xml_block(
            self._results(), [], exclude_names={'Cs-137', 'Co-60', 'Eu-152'})
        self.assertEqual(blocks, [])   # U-235 not identified, rest excluded

    def test_unknown_peaks_remark(self):
        import data_recorder_node as dr
        _, remark = dr.screening_xml_block(
            [], [{'energy_keV': 187.2, 'snr': 35.7}])
        self.assertIn('187.2 keV', remark)
        self.assertIn('unidentified', remark)

    def test_empty_inputs(self):
        import data_recorder_node as dr
        self.assertEqual(dr.screening_xml_block([], []), ([], ''))


class TestHeatmapBands(unittest.TestCase):
    """Dynamic imaging bands (spherical_heatmap_node pure helpers)."""

    LIB = {'Eu-152': {'representative_keV': 1004.73},
           'K-40': {'representative_keV': 1460.82}}

    def test_parse_identified_msg(self):
        import spherical_heatmap_node as hm
        self.assertEqual(hm.parse_identified_msg("Eu-152:0.83|Cs-137:1.00"),
                         ['Eu-152', 'Cs-137'])
        self.assertEqual(hm.parse_identified_msg("none"), [])
        self.assertEqual(hm.parse_identified_msg(""), [])

    def test_identified_nuclide_gets_a_band(self):
        import spherical_heatmap_node as hm
        bands = hm.identified_bands(['Eu-152'], self.LIB, window_kev=30.0)
        self.assertIn('Eu-152', bands)
        self.assertAlmostEqual(bands['Eu-152']['energy'], 1004.73, places=2)
        self.assertEqual(bands['Eu-152']['window'], 30.0)
        # defaults preserved
        self.assertIn('Cs-137', bands)
        self.assertIn('Co-60', bands)

    def test_default_isotopes_not_duplicated(self):
        import spherical_heatmap_node as hm
        bands = hm.identified_bands(['Cs-137', 'Co-60'], self.LIB)
        self.assertEqual(bands['Cs-137'], hm.ISOTOPE_PEAKS['Cs-137'])

    def test_co60_keeps_the_wide_band_even_with_cs137_present(self):
        """REGRESSION GUARD for a reverted change (2026-09-03): narrowing the
        Co-60 band to the 1332 photopeak in mixed Cs+Co fields starved the
        imaging statistics (~10x fewer events) and WORSENED the hotspot bias;
        the suspected Cs+Cs sum-line contamination was measured to not exist
        (Cs-only run: ~zero counts at 1324 keV). Imaging wants events - the
        wide band stays, mixed field or not."""
        import spherical_heatmap_node as hm
        for names in (['Co-60'], ['Cs-137', 'Co-60']):
            bands = hm.identified_bands(names, self.LIB)
            self.assertEqual(bands['Co-60'], hm.ISOTOPE_PEAKS['Co-60'])

    def test_unknown_name_ignored(self):
        import spherical_heatmap_node as hm
        bands = hm.identified_bands(['Xx-999'], self.LIB)
        self.assertNotIn('Xx-999', bands)
        self.assertEqual(len(bands), len(hm.ISOTOPE_PEAKS))

    def test_imaging_band_override(self):
        # A nuclide may image a line CLUSTER instead of its representative
        # line (Eu-152: 964+1086+1112 keV, ~38% of decays).
        import spherical_heatmap_node as hm
        lib = {'Eu-152': {'representative_keV': 1408.01,
                          'imaging_keV': 1038.0,
                          'imaging_window_kev': 80.0}}
        bands = hm.identified_bands(['Eu-152'], lib, window_kev=30.0)
        self.assertEqual(bands['Eu-152'], {'energy': 1038.0, 'window': 80.0})

    def test_real_library_eu152_has_cluster_band(self):
        import spherical_heatmap_node as hm
        lib = isotope_id.load_nuclide_library(LIB_PATH)
        bands = hm.identified_bands(['Eu-152'], lib['nuclides'])
        b = bands['Eu-152']
        # band must cover the 964-1112 keV cluster
        self.assertLessEqual(b['energy'] - b['window'], 964.06)
        self.assertGreaterEqual(b['energy'] + b['window'], 1112.07)


if __name__ == '__main__':
    unittest.main()
