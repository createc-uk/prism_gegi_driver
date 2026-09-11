#!/usr/bin/env python
"""
Per-Isotope 2D Heatmap Plotter - Projects spherical heatmap onto Y-Z plane.

Connects directly to the Prism messaging backend (NATS by default, same as
the C++ driver and the Python processing nodes) and subscribes to:
  gegi.heatmap.cloud (binary) + gegi.heatmap.cloud_meta (JSON) - spherical
      score distribution (point cloud + field-layout metadata)
  gegi.heatmap.source_directions (JSON) - peak directions
  gegi.heatmap.source_isotopes (text) - isotope identification labels
  gegi.activity.results (JSON) - per-isotope activity, for dose-rate weighting

Produces a 2D heatmap view (Y vs Z) with isotope-labeled peak markers,
matching the legacy live_heatmap_node visualization style.

Prerequisites:
  - The Prism Python bindings must be built and installed (see project
    README) -- this replaces the old `pip install roslibpy` requirement.
  - matplotlib, numpy, scipy

Usage:
  python plot_live_2d_heatmap.py --protocol nats --server localhost --port 4222 --radius 0.5
"""
import json
import os
import struct
import sys
import threading

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.ticker import MultipleLocator
from matplotlib.colors import Normalize
from scipy.ndimage import gaussian_filter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "src", "phds_gegi_driver"))

import prism


def parse_cloud_binary_xyz_score(data, meta):
    """Extract xyz positions and per-isotope scores from a raw Prism
    heatmap-cloud binary payload, using the field layout advertised by the
    most recent cloud_meta message."""
    fields = meta.get("fields", [])
    point_step = meta.get("point_step", 28)
    # Fixed offsets matching spherical_heatmap_node's _publish_cloud layout:
    # x=0, y=4, z=8, rgb=12, intensity=16, cs137=20, co60=24 (4-byte floats)
    x_off, y_off, z_off = 0, 4, 8
    has_cs137 = 'cs137' in fields
    has_co60 = 'co60' in fields
    cs137_off = 20
    co60_off = 24
    has_intensity = 'intensity' in fields
    intensity_off = 16

    points = []
    scores_cs137 = []
    scores_co60 = []
    scores_total = []
    for i in range(0, len(data), point_step):
        if i + z_off + 4 > len(data):
            break
        x = struct.unpack_from('<f', data, i + x_off)[0]
        y = struct.unpack_from('<f', data, i + y_off)[0]
        z = struct.unpack_from('<f', data, i + z_off)[0]
        points.append([x, y, z])

        if has_cs137 and i + cs137_off + 4 <= len(data):
            scores_cs137.append(struct.unpack_from('<f', data, i + cs137_off)[0])
        else:
            scores_cs137.append(0.0)

        if has_co60 and i + co60_off + 4 <= len(data):
            scores_co60.append(struct.unpack_from('<f', data, i + co60_off)[0])
        else:
            scores_co60.append(0.0)

        if has_intensity and i + intensity_off + 4 <= len(data):
            scores_total.append(struct.unpack_from('<f', data, i + intensity_off)[0])
        else:
            scores_total.append(0.0)

    pts = np.array(points) if points else np.zeros((0, 3))
    return pts, {
        'cs137': np.array(scores_cs137),
        'co60': np.array(scores_co60),
        'total': np.array(scores_total),
    }


class PerIsotopeHeatmapPlotter(object):
    def __init__(self, app, result, connection, cloud_topic, cloud_meta_topic,
                 peak_topic, isotope_topic, radius):
        self.radius = radius
        self.points = None
        self.iso_scores = None
        self.peaks = []
        self.isotope_text = ""
        self.activity_data = {}  # isotope_name -> dose_rate_uSv_h
        # Specific gamma-ray constants (uSv*m^2 / MBq*h)
        self.gamma_constants = {'Cs-137': 0.0771, 'Co-60': 0.3059}
        self.lock = threading.Lock()
        self.new_data = False
        self._latest_meta = None

        # Grid for 2D projection
        self.grid_res = 200
        self.grid_extent = radius * 1.1  # slightly larger than sphere radius

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

        activity_cfg = prism.TextReceiverConfig()
        activity_cfg.source = "gegi.activity.results"
        self.activity_listener = app.create_text_receiver(result, connection, activity_cfg)
        self.activity_listener.on_receive(self._activity_cb)
        self.activity_listener.start()

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
        pts, iso_scores = parse_cloud_binary_xyz_score(data, meta)
        with self.lock:
            self.points = pts
            self.iso_scores = iso_scores
            self.new_data = True

    def _peaks_cb(self, message, source=None):
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            return
        peaks = []
        for p in payload.get('points', []):
            peaks.append([p.get('x', 0), p.get('y', 0), p.get('z', 0)])
        with self.lock:
            self.peaks = peaks

    def _isotope_cb(self, message, source=None):
        with self.lock:
            self.isotope_text = message

    def _activity_cb(self, message, source=None):
        try:
            data = json.loads(message)
            dose_rates = {}
            # Accumulate activity per display isotope (sum peaks for Co60)
            activities = {}
            for iso in data.get('isotopes', []):
                name = iso.get('isotope', '')
                activity_mbq = iso.get('activity_MBq', 0.0)
                if 'Cs137' in name:
                    display = 'Cs-137'
                elif 'Co60' in name:
                    display = 'Co-60'
                else:
                    display = name
                # For Co-60 both peaks give the same source activity; take max
                if display in activities:
                    activities[display] = max(activities[display], activity_mbq)
                else:
                    activities[display] = activity_mbq
            # Convert activity to dose rate: D_dot = Gamma * A / d^2
            d = self.radius  # source distance = sphere radius
            for display, a_mbq in activities.items():
                gamma = self.gamma_constants.get(display, 0.077)
                dose_rates[display] = gamma * a_mbq / (d * d)
            with self.lock:
                self.activity_data = dose_rates
        except (ValueError, TypeError):
            pass

    def _project_to_2d_grid(self, points, scores):
        """Project 3D sphere points onto Y-Z plane and create a 2D heatmap."""
        extent = self.grid_extent
        res = self.grid_res

        grid = np.zeros((res, res), dtype=np.float64)
        counts = np.zeros((res, res), dtype=np.float64)

        y_edges = np.linspace(-extent, extent, res + 1)
        z_edges = np.linspace(-extent, extent, res + 1)

        if len(points) == 0:
            return grid

        # Bin points by their Y-Z coordinates
        y_idx = np.digitize(points[:, 1], y_edges) - 1
        z_idx = np.digitize(points[:, 2], z_edges) - 1

        valid = (y_idx >= 0) & (y_idx < res) & (z_idx >= 0) & (z_idx < res)

        for i in range(len(points)):
            if valid[i]:
                grid[z_idx[i], y_idx[i]] += scores[i]
                counts[z_idx[i], y_idx[i]] += 1.0

        # Average where we have counts
        mask = counts > 0
        grid[mask] /= counts[mask]

        # Smooth for visual appearance
        grid = gaussian_filter(grid, sigma=3.0)

        # Correct for sphere-to-plane projection distortion.
        yc = np.linspace(-extent, extent, res)
        zc = np.linspace(-extent, extent, res)
        YY, ZZ = np.meshgrid(yc, zc)
        r2 = YY**2 + ZZ**2
        R2 = self.radius**2
        cos_factor = np.sqrt(np.clip(1.0 - r2 / R2, 0.0, 1.0))
        grid *= cos_factor
        # Zero outside sphere
        grid[r2 > R2] = 0.0

        return grid

    def _parse_isotope_labels(self, text):
        """Parse isotope text - node publishes pipe-separated 'Cs-137:150|Co-60:45'.
        Returns list of (name, count) tuples."""
        labels = []
        if not text or text == 'none':
            return labels
        parts = text.split('|')
        for part in parts:
            part = part.strip()
            # Format: "Isotope-Name:count"
            if ':' in part:
                name_part, count_part = part.rsplit(':', 1)
                try:
                    count = int(count_part)
                except ValueError:
                    count = 1
                name = name_part.strip()
            else:
                name = part
                count = 1
            # Validate isotope name
            matched = False
            for iso in ['Cs-137', 'Co-60', 'Am-241', 'Ba-133', 'Na-22']:
                if iso in name:
                    labels.append((iso, count))
                    matched = True
                    break
            if not matched and name:
                labels.append(('---', count))
        return labels

    def run(self):
        print("Connected. Waiting for heatmap data...")

        fig, ax = plt.subplots(figsize=(8, 8), facecolor='black')
        ax.set_facecolor('black')
        ax.set_xlabel("Y (left/right) [m]", color='white')
        ax.set_ylabel("Z (up/down) [m]", color='white')
        ax.set_title("Per-Isotope Heatmap", color='white', fontsize=14)
        ax.tick_params(colors='white')
        for spine in ax.spines.values():
            spine.set_color('white')

        extent = self.grid_extent
        im = [None]

        def update(frame):
            with self.lock:
                if not self.new_data or self.points is None:
                    return []
                pts = self.points.copy()
                iso_scores = {k: v.copy() for k, v in self.iso_scores.items()}
                peaks = list(self.peaks)
                iso_text = self.isotope_text
                self.new_data = False

            if len(pts) == 0:
                return []

            isotope_labels = self._parse_isotope_labels(iso_text)

            # Get dose-rate-based amplitudes for weighting
            with self.lock:
                dose_rates = dict(self.activity_data)

            # Collect per-peak amplitudes from dose rate (uSv/h)
            peak_amps = []
            for i in range(len(peaks)):
                if i < len(isotope_labels):
                    name, count = isotope_labels[i]
                else:
                    name, count = '---', 1
                # Use dose rate from activity node if available
                dr = dose_rates.get(name, 0.0)
                peak_amps.append((name, dr if dr > 0 else float(count)))

            max_amp = max(amp for _, amp in peak_amps) if peak_amps else 1.0
            if max_amp < 1e-6:
                max_amp = 1.0

            # Project per-isotope scores onto 2D grid
            grid_cs137 = self._project_to_2d_grid(pts, iso_scores.get('cs137', np.zeros(len(pts))))
            grid_co60 = self._project_to_2d_grid(pts, iso_scores.get('co60', np.zeros(len(pts))))

            # For each peak, extract a localized region from its isotope grid
            res = self.grid_res
            yc = np.linspace(-extent, extent, res)
            zc = np.linspace(-extent, extent, res)
            YY, ZZ = np.meshgrid(yc, zc)
            grid = np.zeros((res, res), dtype=np.float64)

            blob_radius = 0.10  # meters - tighter blob for better separation

            for i, peak in enumerate(peaks):
                py, pz = peak[1], peak[2]
                if i < len(peak_amps):
                    name, amp_val = peak_amps[i]
                    amp = amp_val / max_amp
                else:
                    name, amp = '---', 0.5

                # Select the right isotope grid
                if 'Cs-137' in name:
                    src_grid = grid_cs137
                elif 'Co-60' in name:
                    src_grid = grid_co60
                else:
                    src_grid = np.maximum(grid_cs137, grid_co60)

                # Circular mask around peak + Gaussian falloff (tight sigma for separation)
                dist = np.sqrt((YY - py)**2 + (ZZ - pz)**2)
                sigma = blob_radius * 0.4
                falloff = np.exp(-0.5 * (dist / sigma)**2)
                mask_region = falloff * src_grid * amp

                grid = np.maximum(grid, mask_region)

            # Normalize to [0, 1]
            gmax = grid.max()
            if gmax > 1e-9:
                grid /= gmax

            ax.cla()
            ax.set_facecolor('black')
            ax.set_xlabel("Y (left/right) [m]", color='white')
            ax.set_ylabel("Z (up/down) [m]", color='white')
            ax.set_title("Per-Isotope Heatmap", color='white', fontsize=14)
            ax.tick_params(colors='white')
            for spine in ax.spines.values():
                spine.set_color('white')

            # Display with 'jet' colormap (blue=low, red=high)
            im[0] = ax.imshow(grid, extent=[-extent, extent, -extent, extent],
                              origin='lower', cmap='jet', interpolation='bilinear',
                              aspect='equal', vmin=0, vmax=1)

            # Plot peak markers with isotope labels (real-world coordinates)
            for i, peak in enumerate(peaks):
                py, pz = peak[1], peak[2]
                if i < len(isotope_labels):
                    label, count = isotope_labels[i]
                else:
                    label, count = '---', 0

                ax.scatter(py, pz, marker='*', c='gold', s=250,
                           edgecolors='black', linewidths=0.8, zorder=10)

                # peak positions are now real-world (ray-plane) coordinates
                label_text = "{}\n({:.2f}cm, {:.2f}cm)".format(label, py*100, pz*100)
                offset_y = 0.05 if py >= 0 else -0.05
                offset_z = 0.06

                ax.annotate(label_text, xy=(py, pz),
                            xytext=(py + offset_y, pz + offset_z),
                            color='black', fontsize=10, fontweight='bold',
                            ha='center', va='bottom',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='white', alpha=0.7, edgecolor='black'),
                            arrowprops=dict(arrowstyle='-', color='black', lw=1.5))

            ax.set_xlim(-extent, extent)
            ax.set_ylim(-extent, extent)
            ax.xaxis.set_major_locator(MultipleLocator(0.05))
            ax.yaxis.set_major_locator(MultipleLocator(0.05))

            return []

        ani = animation.FuncAnimation(fig, update, interval=2000, blit=False)
        plt.tight_layout()
        plt.show()

    def stop(self):
        self.meta_listener.stop()
        self.cloud_listener.stop()
        self.peaks_listener.stop()
        self.isotope_listener.stop()
        self.activity_listener.stop()


def main():
    app = prism.Application("plot_live_2d_heatmap", "Per-isotope 2D heatmap plotter", sys.argv)
    app.add_string_option("PlotLive2dHeatmap", "cloud-topic", "Heatmap point-cloud binary topic",
                           "gegi.heatmap.cloud")
    app.add_string_option("PlotLive2dHeatmap", "cloud-meta-topic", "Heatmap point-cloud metadata topic",
                           "gegi.heatmap.cloud_meta")
    app.add_string_option("PlotLive2dHeatmap", "peak-topic", "Source-directions topic",
                           "gegi.heatmap.source_directions")
    app.add_string_option("PlotLive2dHeatmap", "isotope-topic", "Isotope ID topic",
                           "gegi.heatmap.source_isotopes")
    app.add_float_option("PlotLive2dHeatmap", "radius", "Sphere radius (m)", 0.5)

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    connection = app.create_connection(result)
    if connection is None:
        print("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    plotter = PerIsotopeHeatmapPlotter(app, result, connection,
                                        result.get_string("cloud-topic"),
                                        result.get_string("cloud-meta-topic"),
                                        result.get_string("peak-topic"),
                                        result.get_string("isotope-topic"),
                                        result.get_float("radius"))
    plotter.run()
    plotter.stop()
    connection.close()


if __name__ == "__main__":
    main()
