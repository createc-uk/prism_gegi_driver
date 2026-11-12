#!/usr/bin/env python3
"""Regression checks for GeGi/ROS lifecycle and cross-topic frame integrity."""
import csv
import hashlib
import json
import os
import struct
import sys
import threading
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))
from prism_command_channel import CommandServer
from data_recorder_node import DataRecorderNode


class Receiver:
    def on_receive(self, handler):
        self.handler = handler
    def start(self):
        pass
    def stop(self):
        pass
    def deliver(self, obj):
        self.handler(json.dumps(obj), "test.command")


class Sender:
    def __init__(self):
        self.outputs = []
    def send(self, payload):
        self.outputs.append(json.loads(payload))
        return True


class FakeClient:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
    def call(self, params, timeout=5.0):
        self.calls.append(params)
        if self.fail and params['command'] == self.fail:
            return {'success': False, 'message': 'mock rejected'}
        return {'success': True, 'message': 'mock accepted'}


class TestCommandReplay(unittest.TestCase):
    def test_duplicate_id_executes_only_once(self):
        receiver, sender = Receiver(), Sender()
        server = CommandServer(receiver, sender)
        calls = []
        def handler(command):
            calls.append(command)
            return True, 'ok'
        server.on('start', handler)
        server.start()
        message = {'command': 'start', 'request_id': 'id-123'}
        receiver.deliver(message)
        receiver.deliver(message)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sender.outputs[0], sender.outputs[1])
        receiver.deliver({'command': 'clear', 'request_id': 'id-123'})
        self.assertFalse(sender.outputs[-1]['success'])
        self.assertEqual(len(calls), 1)
        server.stop()


class TestRunStart(unittest.TestCase):
    def make_node(self):
        node = DataRecorderNode.__new__(DataRecorderNode)
        node.lock = threading.Lock()
        node.recording = False
        node.output_dir = '/tmp/not-used-gegi'
        node._detector_client = FakeClient()
        node._node_clear_clients = [(FakeClient(), 'clear', 'gegi.spectrum.command')]
        node._start_recording = mock.Mock()
        return node

    def test_deep_clear_precedes_start(self):
        node = self.make_node()
        with mock.patch('data_recorder_node.time.sleep'):
            good, message = node._handle_start({'duration_minutes': 5})
        self.assertTrue(good, message)
        self.assertEqual([r['command'] for r in node._detector_client.calls],
                         ['clear_data_and_windows', 'start_timed_acquisition'])
        node._start_recording.assert_called_once_with(5)

    def test_deep_clear_failure_aborts_without_fallback(self):
        node = self.make_node()
        node._detector_client = FakeClient('clear_data_and_windows')
        with mock.patch('data_recorder_node.time.sleep'):
            good, message = node._handle_start({'duration_minutes': 5})
        self.assertFalse(good, message)
        self.assertEqual([r['command'] for r in node._detector_client.calls],
                         ['clear_data_and_windows'])
        node._start_recording.assert_not_called()

    def test_invalid_duration_never_contacts_detector(self):
        node = self.make_node()
        good, _ = node._handle_start({'duration_minutes': 12})
        self.assertFalse(good)
        self.assertEqual(node._detector_client.calls, [])



class TestRunLevelCSV(unittest.TestCase):
    def test_csv_uses_whole_run_aggregation_with_deadtime(self):
        with tempfile.TemporaryDirectory() as temp:
            node = DataRecorderNode.__new__(DataRecorderNode)
            node.output_dir = temp
            activities = [
                {'timestamp': 20.0, 'live_time_s': 60.0, 'isotopes': [
                    {'isotope': 'Cs137', 'net_corrected': 1000.0,
                     'activity_MBq': 1.0, 'energy_keV': 661.7,
                     'valid': True, 'gross_counts': 1300.0,
                     'background_counts': 300.0}]},
                {'timestamp': 80.0, 'live_time_s': 60.0, 'isotopes': [
                    {'isotope': 'Cs137', 'net_corrected': 1100.0,
                     'activity_MBq': 1.1, 'energy_keV': 661.7,
                     'valid': True, 'gross_counts': 1400.0,
                     'background_counts': 300.0}]},
            ]
            node._save_activity_csv('sample', activities, {
                'dead_time_percent': 5, 'count_rate_hz': 100,
            }, measurement_id='measurement-test', dt_factor=1.05)
            with open(os.path.join(temp, 'sample_activity.csv')) as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['measurement_id'], 'measurement-test')
            self.assertEqual(rows[0]['isotope'], 'Cs137')
            self.assertAlmostEqual(float(rows[0]['net_peak_area']), 2100.0)
            self.assertAlmostEqual(float(rows[0]['net_corrected']), 2205.0)



class TestCloudMetadataPairing(unittest.TestCase):
    def make_node(self):
        node = DataRecorderNode.__new__(DataRecorderNode)
        node.lock = threading.Lock()
        node.recording = True
        node.recording_start = 10.0
        node.heatmap_cloud = None
        node._cloud_meta_by_digest = {}
        node._cloud_blobs_by_digest = {}
        return node

    def frame(self, stamp, value):
        raw = struct.pack('<7f', 0.5, value, 0.3, 0, 1, 0.8, 0.1)
        meta = {'stamp': stamp, 'point_step': 28, 'n_points': 1,
                'fields': ['x','y','z','rgb','intensity','cs137','co60'],
                'byte_length': len(raw), 'blob_sha256': hashlib.sha256(raw).hexdigest()}
        return raw, meta

    def test_both_arrival_orders(self):
        node = self.make_node()
        raw, meta = self.frame(11.0, 3.0)
        node._on_cloud(raw)
        self.assertIsNone(node.heatmap_cloud)
        node._on_cloud_meta(json.dumps(meta))
        self.assertAlmostEqual(node.heatmap_cloud[0, 1], 3.0)
        node.heatmap_cloud = None
        raw, meta = self.frame(12.0, 4.0)
        node._on_cloud_meta(json.dumps(meta))
        self.assertIsNone(node.heatmap_cloud)
        node._on_cloud(raw)
        self.assertAlmostEqual(node.heatmap_cloud[0, 1], 4.0)

    def test_no_stale_or_cross_run_pairing(self):
        node = self.make_node()
        older_raw, older_meta = self.frame(9.0, 1.0)
        new_raw, new_meta = self.frame(11.0, 2.0)
        node._on_cloud_meta(json.dumps(older_meta))
        node._on_cloud(new_raw)
        self.assertIsNone(node.heatmap_cloud)
        node._on_cloud_meta(json.dumps(new_meta))
        self.assertAlmostEqual(node.heatmap_cloud[0, 1], 2.0)
        self.assertEqual(len(node._cloud_meta_by_digest), 0)


if __name__ == '__main__':
    unittest.main()
