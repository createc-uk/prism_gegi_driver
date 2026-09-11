#!/usr/bin/env python
"""
Live Spherical Heatmap Plotter - Runs natively on Windows (or any machine)
via Prism.

Connects directly to the Prism messaging backend (NATS by default, same as
the C++ driver and the Python processing nodes) and subscribes to the
heatmap point-cloud (binary + JSON metadata), source-directions, and
source-isotopes topics. Displays a 3D scatter plot colored by
back-projection score.

Prerequisites:
  - The Prism Python bindings must be built and installed (see project
    README) -- this replaces the old `pip install roslibpy` requirement.
  - matplotlib, numpy

Usage:
  python plot_live_heatmap.py --protocol nats --server localhost --port 4222
"""
import os
import struct
import sys
import threading

import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
import matplotlib.animation as animation

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "src", "phds_gegi_driver"))

import json

import prism


def parse_cloud_binary(data, meta):
    """Extract xyz and score from a raw Prism heatmap-cloud binary payload,
    using the field layout advertised by the most recent cloud_meta message."""
    fields = meta.get("fields", [])
    point_step = meta.get("point_step", 28)
    # Fixed offsets matching spherical_heatmap_node's _publish_cloud layout:
    # x=0, y=4, z=8, rgb=12, intensity=16, cs137=20, co60=24 (4-byte floats)
    x_off, y_off, z_off = 0, 4, 8
    has_rgb = 'rgb' in fields
    rgb_off = 12

    points = []
    scores = []
    for i in range(0, len(data), point_step):
        if i + z_off + 4 > len(data):
            break
        x = struct.unpack_from('<f', data, i + x_off)[0]
        y = struct.unpack_from('<f', data, i + y_off)[0]
        z = struct.unpack_from('<f', data, i + z_off)[0]
        points.append([x, y, z])

        if has_rgb and i + rgb_off + 4 <= len(data):
            rgb_bytes = struct.unpack_from('<I', data, i + rgb_off)[0]
            r = (rgb_bytes >> 16) & 0xFF
            scores.append(r / 255.0)
        else:
            scores.append(0.5)

    return np.array(points) if points else np.zeros((0, 3)), np.array(scores)


class LiveHeatmapPlotter(object):
    def __init__(self, app, result, connection, cloud_topic, cloud_meta_topic,
                 peak_topic, isotope_topic):
        self.points = None
        self.scores = None
        self.peak_dirs = []
        self.isotope_text = ""
        self.lock = threading.Lock()
        self.new_data = False
        self._latest_meta = None

        meta_cfg = prism.TextReceiverConfig()
        meta_cfg.source = cloud_meta_topic
        self.meta_listener = app.create_text_receiver(result, connection, meta_cfg)
        self.meta_listener.on_receive(self._meta_cb)
        self.meta_listener.start()

        cloud_cfg = prism.BinaryReceiverConfig()
        cloud_cfg.source = cloud_topic
        self.cloud_listener = app.create_binary_receiver(result, connection, cloud_cfg)
        self.cloud_listener.on_receive(self._cloud_cb)
        self.cloud_listener.start()

        peaks_cfg = prism.TextReceiverConfig()
        peaks_cfg.source = peak_topic
        self.peaks_listener = app.create_text_receiver(result, connection, peaks_cfg)
        self.peaks_listener.on_receive(self._peaks_cb)
        self.peaks_listener.start()

        isotope_cfg = prism.TextReceiverConfig()
        isotope_cfg.source = isotope_topic
        self.isotope_listener = app.create_text_receiver(result, connection, isotope_cfg)
        self.isotope_listener.on_receive(self._isotope_cb)
        self.isotope_listener.start()

    def _meta_cb(self, message, source=None):
        try:
            meta = json.loads(message)
        except (ValueError, TypeError):
            return
        with self.lock:
            self._latest_meta = meta

    def _cloud_cb(self, data, source=None):
        with self.lock:
            meta = self._latest_meta
        if meta is None:
            # Binary payload arrived before any metadata - can't parse it yet.
            return
        pts, scores = parse_cloud_binary(data, meta)
        with self.lock:
            self.points = pts
            self.scores = scores
            self.new_data = True

    def _peaks_cb(self, message, source=None):
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            return
        dirs = []
        for p in payload.get('points', []):
            dirs.append([p.get('x', 0), p.get('y', 0), p.get('z', 0)])
        with self.lock:
            self.peak_dirs = dirs

    def _isotope_cb(self, message, source=None):
        with self.lock:
            self.isotope_text = message

    def run(self):
        print("Connected. Waiting for heatmap data...")

        fig = plt.figure(figsize=(10, 8))
        ax = fig.add_subplot(111, projection='3d')
        ax.set_xlabel("X (m)")
        ax.set_ylabel("Y (m)")
        ax.set_zlabel("Z (m)")
        ax.set_title("GeGI Spherical Heatmap")

        def update(frame):
            with self.lock:
                if not self.new_data or self.points is None:
                    return []
                pts = self.points.copy()
                scores = self.scores.copy()
                peaks = list(self.peak_dirs)
                iso_text = self.isotope_text
                self.new_data = False

            if len(pts) == 0:
                return []

            ax.cla()
            ax.set_xlabel("X (m)")
            ax.set_ylabel("Y (m)")
            ax.set_zlabel("Z (m)")

            ax.scatter(pts[:, 0], pts[:, 1], pts[:, 2],
                       c=scores, cmap='hot', s=2, alpha=0.6)

            if peaks:
                peak_arr = np.array(peaks)
                ax.scatter(peak_arr[:, 0], peak_arr[:, 1], peak_arr[:, 2],
                           c='gold', s=200, marker='*', edgecolors='black',
                           linewidths=0.8, zorder=10)

            ax.set_title("GeGI Spherical Heatmap")
            if iso_text:
                ax.text2D(0.5, 0.92, iso_text, transform=ax.transAxes,
                          fontsize=10, color='black', fontweight='bold',
                          ha='center', va='top')

            max_range = abs(pts).max() * 1.2 if len(pts) > 0 else 0.6
            ax.set_xlim(-max_range, max_range)
            ax.set_ylim(-max_range, max_range)
            ax.set_zlim(-max_range, max_range)
            return []

        ani = animation.FuncAnimation(fig, update, interval=2000, blit=False)
        plt.tight_layout()
        plt.show()

    def stop(self):
        self.meta_listener.stop()
        self.cloud_listener.stop()
        self.peaks_listener.stop()
        self.isotope_listener.stop()


def main():
    app = prism.Application("plot_live_heatmap", "Live GeGI spherical heatmap plotter", sys.argv)
    app.add_string_option("PlotLiveHeatmap", "cloud-topic", "Heatmap point-cloud binary topic",
                           "gegi.heatmap.cloud")
    app.add_string_option("PlotLiveHeatmap", "cloud-meta-topic", "Heatmap point-cloud metadata topic",
                           "gegi.heatmap.cloud_meta")
    app.add_string_option("PlotLiveHeatmap", "peak-topic", "Source-directions topic",
                           "gegi.heatmap.source_directions")
    app.add_string_option("PlotLiveHeatmap", "isotope-topic", "Isotope ID topic",
                           "gegi.heatmap.source_isotopes")

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    connection = app.create_connection(result)
    if connection is None:
        print("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    plotter = LiveHeatmapPlotter(app, result, connection,
                                  result.get_string("cloud-topic"),
                                  result.get_string("cloud-meta-topic"),
                                  result.get_string("peak-topic"),
                                  result.get_string("isotope-topic"))
    plotter.run()
    plotter.stop()
    connection.close()


if __name__ == "__main__":
    main()
