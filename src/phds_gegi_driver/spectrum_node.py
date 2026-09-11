#!/usr/bin/env python
"""
Spectrum Node - Standalone raw energy spectrum accumulator.

Subscribes to a Prism topic (default: gegi.driver.energy_deposit) carrying a
DoubleValue for every single-site and two-site interaction energy, published
by the GeGI driver. Accumulates into a configurable histogram and publishes
a Spectrum message (see prism_messages.make_spectrum) periodically.

CLI options (defaults mirror the old ROS ~params of the same purpose):
  --calibration-file (str): Path to EnergyCal.csv (energy bin edges in keV)
  --publish-rate-hz (float): Spectrum publish rate (default: 4.0 Hz)
  --energy-topic (str): Input topic (default: gegi.driver.energy_deposit)
  --spectrum-topic (str): Output topic (default: gegi.spectrum.histogram)
  --detector-frame (str): Frame ID for spectrum header (default: detector)
  --use-run-info-deadtime (bool): Populate dead_time_ms from the detector's
      periodically-published run-info dead_time_percent (default: false)
  --run-info-topic (str): Topic carrying periodic RunInfo state from the
      detector driver (default: gegi.detector.run_info)
  --use-dead-time-topic (bool): Use a shared dead-time percentage topic
      instead of the run-info topic in this node (default: false)
  --dead-time-topic (str): Topic carrying dead-time percentage as a
      DoubleValue (default: gegi.detector.dead_time_percent)
  --publish-dead-time-topic (bool): Publish this node's dead-time percentage
      to --dead-time-topic for other nodes to consume (default: false)
  --node-name (str): Used only to build the command-channel topic names
      gegi.<node-name>.command / gegi.<node-name>.command_result
      (default: spectrum)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import logging
import threading
import time

import numpy as np
import prism

import prism_messages as pmsg
from prism_command_channel import CommandServer

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("spectrum_node")


class SpectrumNode(object):
    def __init__(self, app, args, connection):
        self._app = app

        cal_path = args.get_string("calibration-file")
        self.publish_rate = args.get_float("publish-rate-hz")
        energy_topic = args.get_string("energy-topic")
        spectrum_topic = args.get_string("spectrum-topic")
        self.detector_frame = args.get_string("detector-frame")
        # IMPORTANT: run-info shares the same detector TCP channel as event
        # streaming on the C++ driver side. The driver mitigates contention by
        # publishing run-info periodically (2s idle / 30s while acquisition is
        # active) rather than us polling it on demand.
        self.use_run_info_deadtime = args.get_bool("use-run-info-deadtime")
        self.run_info_topic = args.get_string("run-info-topic")
        self.use_dead_time_topic = args.get_bool("use-dead-time-topic")
        self.dead_time_topic = args.get_string("dead-time-topic")
        self.publish_dead_time_topic = args.get_bool("publish-dead-time-topic")
        node_name = args.get_string("node-name")

        self._last_dead_time_percent = 0.0
        self._dead_time_lock = threading.Lock()

        # Load energy calibration (bin edges in keV)
        self.bin_edges = self._load_calibration(cal_path)
        self.n_bins = len(self.bin_edges)
        logger.info("Spectrum node: %d bins, publish rate %.1f Hz", self.n_bins, self.publish_rate)

        self.spectrum = np.zeros(self.n_bins, dtype=np.uint32)
        self.lock = threading.Lock()
        self.seq = 0

        # -- Senders/receivers ------------------------------------------------
        spectrum_cfg = prism.TextSenderConfig()
        spectrum_cfg.destination = spectrum_topic
        self.pub = app.create_text_sender(args, connection, spectrum_cfg)

        energy_cfg = prism.TextReceiverConfig()
        energy_cfg.source = energy_topic
        self.sub = app.create_text_receiver(args, connection, energy_cfg)
        self.sub.on_receive(self._on_energy)
        self.sub.start()

        self._dead_time_sub = None
        self._dead_time_pub = None

        if self.use_dead_time_topic:
            dt_recv_cfg = prism.TextReceiverConfig()
            dt_recv_cfg.source = self.dead_time_topic
            self._dead_time_sub = app.create_text_receiver(args, connection, dt_recv_cfg)
            self._dead_time_sub.on_receive(self._on_dead_time_percent)
            self._dead_time_sub.start()
            logger.info("Spectrum node: using shared dead-time topic %s", self.dead_time_topic)

        if self.publish_dead_time_topic:
            dt_send_cfg = prism.TextSenderConfig()
            dt_send_cfg.destination = self.dead_time_topic
            self._dead_time_pub = app.create_text_sender(args, connection, dt_send_cfg)
            logger.info("Spectrum node: publishing dead-time percentage to %s", self.dead_time_topic)

        self._run_info_sub = None
        if self.use_run_info_deadtime:
            run_info_cfg = prism.TextReceiverConfig()
            run_info_cfg.source = self.run_info_topic
            self._run_info_sub = app.create_text_receiver(args, connection, run_info_cfg)
            self._run_info_sub.on_receive(self._on_run_info)
            self._run_info_sub.start()
            logger.info("Spectrum node: using %s for dead-time", self.run_info_topic)

        # -- Command channel (replaces the old ~clear Trigger service) --------
        command_topic = "gegi.{}.command".format(node_name)
        command_result_topic = "gegi.{}.command_result".format(node_name)

        command_recv_cfg = prism.TextReceiverConfig()
        command_recv_cfg.source = command_topic
        command_receiver = app.create_text_receiver(args, connection, command_recv_cfg)

        command_result_cfg = prism.TextSenderConfig()
        command_result_cfg.destination = command_result_topic
        command_result_sender = app.create_text_sender(args, connection, command_result_cfg)

        self.command_server = CommandServer(command_receiver, command_result_sender)
        self.command_server.on("clear", self._handle_clear)
        self.command_server.start()

        # -- Periodic publish thread (replaces rospy.Timer) --------------------
        self._stop_event = threading.Event()
        self._publish_thread = threading.Thread(target=self._publish_loop, daemon=True)
        self._publish_thread.start()

    def _load_calibration(self, path):
        if not path or not os.path.exists(path):
            logger.warning("No calibration file, using default 1024 bins (0-3000 keV)")
            return np.linspace(0.0, 3000.0, 1024)
        values = []
        with open(path, 'r') as f:
            for line in f:
                text = line.strip()
                if text:
                    values.append(float(text))
        return np.array(values, dtype=np.float64)

    def _on_energy(self, message, source=None):
        try:
            payload = json.loads(message)
        except Exception:
            return
        energy = pmsg.parse_double_value(payload)
        if energy is None:
            return
        idx = np.searchsorted(self.bin_edges, energy, side='right') - 1
        if idx < 0 or idx >= self.n_bins:
            return
        with self.lock:
            self.spectrum[idx] += 1

    def _publish_loop(self):
        period = 1.0 / max(self.publish_rate, 0.1)
        while self._app.is_running() and not self._stop_event.is_set():
            time.sleep(period)
            if not self._app.is_running() or self._stop_event.is_set():
                break
            self._publish()

    def _publish(self):
        period_ms = int(1000.0 / max(self.publish_rate, 0.1))
        dead_time_ms = self._get_dead_time_ms(period_ms)
        stamp = pmsg.now_seconds()
        end_time = pmsg.now_seconds()

        spectrum_counts, total_count = self._snapshot_and_reset()

        payload = pmsg.make_spectrum(
            stamp=stamp,
            frame_id=self.detector_frame,
            seq=self.seq,
            end_time=end_time,
            real_time_ms=period_ms,
            dead_time_ms=dead_time_ms,
            total_count=total_count,
            spectrum=spectrum_counts,
        )
        self.seq += 1

        self.pub.send(json.dumps(payload))

    def _snapshot_and_reset(self):
        """Return the counts accumulated since the last publish, and zero them.

        CONTRACT: the spectrum topic carries per-interval DELTAS, not a running
        total. The data recorder ADDS each message into the spectrum it saves,
        so publishing a cumulative histogram here would make every saved N42
        over-count. Keep this a delta. (Unit-tested.)
        """
        with self.lock:
            counts = self.spectrum.tolist()
            total = int(self.spectrum.sum())
            self.spectrum[:] = 0
        return counts, total

    def _get_dead_time_ms(self, period_ms):
        """Non-blocking: convert the latest cached dead-time % to milliseconds.

        Runs on the publish hot path, so it MUST NOT block. The cached value is
        updated asynchronously by `_on_run_info`/`_on_dead_time_percent`.
        """
        if not self.use_run_info_deadtime and not self.use_dead_time_topic:
            return 0

        with self._dead_time_lock:
            dead_time_percent = self._last_dead_time_percent

        # Re-publish the shared dead-time topic for downstream nodes.
        if self._dead_time_pub is not None:
            self._dead_time_pub.send(json.dumps(pmsg.make_double_value(dead_time_percent)))

        return int(round(period_ms * dead_time_percent / 100.0))

    def _on_run_info(self, message, source=None):
        """Callback for the detector's periodically-published RunInfo state."""
        try:
            payload = json.loads(message)
        except Exception:
            return
        info = pmsg.parse_run_info(payload)
        if not info.get("valid"):
            return
        dead_time_percent = info.get("dead_time_percent")
        if dead_time_percent is None or not np.isfinite(dead_time_percent):
            return
        with self._dead_time_lock:
            self._last_dead_time_percent = max(0.0, min(100.0, float(dead_time_percent)))

    def _on_dead_time_percent(self, message, source=None):
        try:
            payload = json.loads(message)
        except Exception:
            return
        value = pmsg.parse_double_value(payload)
        if value is None or not np.isfinite(value):
            return
        with self._dead_time_lock:
            self._last_dead_time_percent = max(0.0, min(100.0, float(value)))

    def _handle_clear(self, params):
        with self.lock:
            total = int(self.spectrum.sum())
            self.spectrum[:] = 0
        logger.info("Spectrum cleared (%d counts discarded)", total)
        return True, "Cleared {} counts".format(total)

    def stop(self):
        self._stop_event.set()
        self._publish_thread.join(timeout=2.0)
        self.command_server.stop()
        self.sub.stop()
        if self._dead_time_sub is not None:
            self._dead_time_sub.stop()
        if self._run_info_sub is not None:
            self._run_info_sub.stop()


def main():
    app = prism.Application("spectrum_node", "GeGi spectrum accumulator", sys.argv)

    app.add_string_option("Spectrum", "calibration-file", "Path to EnergyCal.csv (energy bin edges in keV)", "")
    app.add_float_option("Spectrum", "publish-rate-hz", "Spectrum publish rate (Hz)", 4.0)
    app.add_string_option("Spectrum", "energy-topic", "Input energy-deposit topic", "gegi.driver.energy_deposit")
    app.add_string_option("Spectrum", "spectrum-topic", "Output spectrum topic", "gegi.spectrum.histogram")
    app.add_string_option("Spectrum", "detector-frame", "Frame ID for spectrum header", "detector")
    app.add_bool_option("Spectrum", "use-run-info-deadtime",
                         "Populate dead_time_ms from the detector run-info topic")
    app.add_string_option("Spectrum", "run-info-topic", "Detector run-info topic", "gegi.detector.run_info")
    app.add_bool_option("Spectrum", "use-dead-time-topic",
                         "Use a shared dead-time percentage topic instead of run-info")
    app.add_string_option("Spectrum", "dead-time-topic", "Shared dead-time percentage topic",
                           "gegi.detector.dead_time_percent")
    app.add_bool_option("Spectrum", "publish-dead-time-topic",
                         "Publish this node's dead-time percentage for other nodes to consume")
    app.add_string_option("Spectrum", "node-name",
                           "Used to build gegi.<node-name>.command(_result) topic names", "spectrum")

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    connection = app.create_connection(result)
    if connection is None:
        logger.error("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    node = SpectrumNode(app, result, connection)

    while app.is_running():
        time.sleep(0.1)

    node.stop()
    connection.close()


if __name__ == "__main__":
    main()
