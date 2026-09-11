#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the spectrum accumulator.

The node's methods are exercised directly on an object built with __new__ (no
rospy.init_node, no master, no detector) — only the attributes the methods
actually touch are set up.

The critical thing under test is the DELTA CONTRACT: /spectrum publishes the
counts accumulated *since the last publish* and then zeroes itself. The data
recorder ADDS each message into the spectrum it saves, so if this ever became
cumulative every saved N42 would over-count. That bug would be silent.
"""
from __future__ import print_function

import json
import os
import sys
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))

import spectrum_node as sn  # noqa: E402
import prism_messages as pmsg  # noqa: E402


def _Energy(value):
    """Builds the JSON-encoded DoubleValue message _on_energy now expects
    (previously a std_msgs/Float64-like stand-in with a `.data` attribute)."""
    return json.dumps(pmsg.make_double_value(value))


def make_node(n_bins=1024, e_max=3000.0):
    """A SpectrumNode with just the state its histogram methods use."""
    node = sn.SpectrumNode.__new__(sn.SpectrumNode)
    node.bin_edges = np.linspace(0.0, e_max, n_bins)
    node.n_bins = n_bins
    node.spectrum = np.zeros(n_bins, dtype=np.uint32)
    node.lock = threading.Lock()
    return node


class TestEnergyBinning(unittest.TestCase):
    def setUp(self):
        self.node = make_node()
        self.width = self.node.bin_edges[1] - self.node.bin_edges[0]

    def _bin_of(self, energy):
        return int(np.searchsorted(self.node.bin_edges, energy, side='right') - 1)

    def test_count_lands_in_the_bin_containing_the_energy(self):
        for energy in (661.7, 1173.2, 1332.5):
            node = make_node()
            node._on_energy(_Energy(energy))
            idx = self._bin_of(energy)
            self.assertEqual(node.spectrum[idx], 1,
                             "%.1f keV must land in its own bin" % energy)
            self.assertEqual(int(node.spectrum.sum()), 1, "exactly one count")

    def test_bin_edge_contains_its_lower_boundary(self):
        edge = self.node.bin_edges[100]
        self.node._on_energy(_Energy(edge))
        self.assertEqual(self.node.spectrum[100], 1)

    def test_repeated_energies_accumulate(self):
        for _ in range(7):
            self.node._on_energy(_Energy(661.7))
        self.assertEqual(int(self.node.spectrum.sum()), 7)

    def test_energy_above_range_falls_into_an_overflow_bin(self):
       
        self.node._on_energy(_Energy(9999.0))
        self.assertEqual(int(self.node.spectrum[-1]), 1,
                         "currently lands in the top (overflow) bin")
        self.assertEqual(int(self.node.spectrum[:-1].sum()), 0,
                         "must not contaminate any in-range bin")

    def test_negative_energy_is_discarded(self):
        self.node._on_energy(_Energy(-5.0))
        self.assertEqual(int(self.node.spectrum.sum()), 0)

    def test_distinct_lines_land_in_distinct_bins(self):
        self.node._on_energy(_Energy(1173.2))
        self.node._on_energy(_Energy(1332.5))
        self.assertNotEqual(self._bin_of(1173.2), self._bin_of(1332.5))
        self.assertEqual(int(self.node.spectrum.sum()), 2)


class TestDeltaContract(unittest.TestCase):
    """/spectrum must publish per-interval deltas and reset."""

    def setUp(self):
        self.node = make_node()

    def test_snapshot_returns_counts_since_last_publish(self):
        for _ in range(5):
            self.node._on_energy(_Energy(661.7))
        counts, total = self.node._snapshot_and_reset()
        self.assertEqual(total, 5)
        self.assertEqual(sum(counts), 5)

    def test_snapshot_zeroes_the_histogram(self):
        self.node._on_energy(_Energy(661.7))
        self.node._snapshot_and_reset()
        self.assertEqual(int(self.node.spectrum.sum()), 0,
                         "histogram MUST be zeroed after publish")

    def test_second_snapshot_is_empty_without_new_events(self):
        self.node._on_energy(_Energy(661.7))
        self.node._snapshot_and_reset()
        counts, total = self.node._snapshot_and_reset()
        self.assertEqual(total, 0, "a delta, not a running total")
        self.assertEqual(sum(counts), 0)

    def test_successive_intervals_report_only_their_own_counts(self):
        """The regression that would silently corrupt every saved N42."""
        for _ in range(3):
            self.node._on_energy(_Energy(661.7))
        _, first = self.node._snapshot_and_reset()

        for _ in range(2):
            self.node._on_energy(_Energy(1173.2))
        _, second = self.node._snapshot_and_reset()

        self.assertEqual(first, 3)
        self.assertEqual(second, 2, "must be 2, not 5 — deltas, not cumulative")

    def test_integrating_the_deltas_reproduces_the_total(self):
        """This is exactly what the data recorder does to build the saved N42."""
        integrated = np.zeros(self.node.n_bins, dtype=np.uint64)
        emitted = 0
        for interval in range(4):
            for _ in range(interval + 1):        # 1, 2, 3, 4 counts per interval
                self.node._on_energy(_Energy(661.7))
                emitted += 1
            counts, _ = self.node._snapshot_and_reset()
            integrated += np.array(counts, dtype=np.uint64)
        self.assertEqual(int(integrated.sum()), emitted)
        self.assertEqual(int(integrated.sum()), 10)


if __name__ == '__main__':
    unittest.main()
