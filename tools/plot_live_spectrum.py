#!/usr/bin/env python
"""
Live Spectrum Plotter - Runs natively on Windows (or any machine) via Prism.

Connects directly to the Prism messaging backend (NATS by default, same as
the C++ driver and the Python processing nodes) and subscribes to the
spectrum, singles-spectrum, and activity-results topics. Displays a
real-time matplotlib energy histogram.

Prerequisites:
  - The Prism Python bindings must be built and installed (see project
    README) -- this replaces the old `pip install roslibpy` requirement.
  - matplotlib, numpy

Usage:
  python plot_live_spectrum.py --protocol nats --server localhost --port 4222
  python plot_live_spectrum.py --cumulative --cal ../config/EnergyCal.csv
"""
import json
import os
import sys
import threading

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "src", "phds_gegi_driver"))

import prism

import prism_messages as pmsg


class LiveSpectrumPlotter(object):
    def __init__(self, app, result, connection, topic, cal_path, cumulative):
        self.cumulative = cumulative
        self.bin_edges = self._load_cal(cal_path)
        self.n_bins = len(self.bin_edges)
        self.counts_cumulative = np.zeros(self.n_bins, dtype=np.float64)
        self.counts_singles = np.zeros(self.n_bins, dtype=np.float64)
        self.lock = threading.Lock()
        self.singles_lock = threading.Lock()
        self.new_data = False
        self.new_singles_data = False

        # Activity results state
        self.activity_lock = threading.Lock()
        self.activity_results = None

        # All-events spectrum
        cfg = prism.TextReceiverConfig()
        cfg.source = topic
        self.listener = app.create_text_receiver(result, connection, cfg)
        self.listener.on_receive(self._cb)
        self.listener.start()

        # Singles-only spectrum
        singles_cfg = prism.TextReceiverConfig()
        singles_cfg.source = "gegi.spectrum_singles.histogram"
        self.singles_listener = app.create_text_receiver(result, connection, singles_cfg)
        self.singles_listener.on_receive(self._singles_cb)
        self.singles_listener.start()

        # Subscribe to activity results
        activity_cfg = prism.TextReceiverConfig()
        activity_cfg.source = "gegi.activity.results"
        self.activity_listener = app.create_text_receiver(result, connection, activity_cfg)
        self.activity_listener.on_receive(self._activity_cb)
        self.activity_listener.start()

    def _load_cal(self, path):
        if path and os.path.exists(path):
            values = []
            with open(path, 'r') as f:
                for line in f:
                    t = line.strip()
                    if t:
                        values.append(float(t))
            return np.array(values)
        return np.linspace(0, 3000, 1024)

    def _cb(self, message, source=None):
        try:
            payload = pmsg.parse_spectrum(json.loads(message))
        except Exception:
            return
        arr = np.array(payload.get('spectrum', []), dtype=np.float64)
        with self.lock:
            n = min(len(arr), self.n_bins)
            self.counts_cumulative[:n] += arr[:n]
            self.new_data = True

    def _singles_cb(self, message, source=None):
        try:
            payload = pmsg.parse_spectrum(json.loads(message))
        except Exception:
            return
        arr = np.array(payload.get('spectrum', []), dtype=np.float64)
        with self.singles_lock:
            n = min(len(arr), self.n_bins)
            self.counts_singles[:n] += arr[:n]
            self.new_singles_data = True

    def _activity_cb(self, message, source=None):
        try:
            data = json.loads(message)
            with self.activity_lock:
                self.activity_results = data
        except (ValueError, TypeError):
            pass

    def run(self):
        print("Connected. Waiting for spectrum data...")

        fig, (ax_cum, ax_snap) = plt.subplots(1, 2, figsize=(16, 5))
        xlim = self.bin_edges[-1] if len(self.bin_edges) > 0 else 3000

        # Left: cumulative
        ax_cum.set_xlabel("Energy (keV)")
        ax_cum.set_ylabel("Counts")
        ax_cum.set_title("Cumulative Spectrum")
        ax_cum.set_xlim(0, xlim)

        # Right: singles-only spectrum
        ax_snap.set_xlabel("Energy (keV)")
        ax_snap.set_ylabel("Counts")
        ax_snap.set_title("Singles-Only Spectrum (Photoelectric)")
        ax_snap.set_xlim(0, xlim)

        widths = np.diff(np.append(self.bin_edges, self.bin_edges[-1] + 2.0))
        bars_cum = ax_cum.bar(self.bin_edges[:self.n_bins], np.zeros(self.n_bins),
                              width=widths[:self.n_bins],
                              color='steelblue', edgecolor='none')
        bars_snap = ax_snap.bar(self.bin_edges[:self.n_bins], np.zeros(self.n_bins),
                                width=widths[:self.n_bins],
                                color='darkorange', edgecolor='none')

        # Isotope ROI colour shading
        isotope_roi_colors = {
            'Cs137': ('red', [655.0, 669.0]),
            'Co60_1173': ('green', [1165.0, 1181.0]),
            'Co60_1332': ('purple', [1325.0, 1340.0]),
        }
        for ax in (ax_cum, ax_snap):
            for iso_name, (color, roi) in isotope_roi_colors.items():
                ax.axvspan(roi[0], roi[1], alpha=0.15, color=color,
                           linewidth=2, edgecolor=color,
                           label=iso_name, zorder=0)
        # Add legend to right panel only (avoid clutter)
        ax_snap.legend(loc='upper right', fontsize=8)

        # Activity annotation text box (upper-right of cumulative plot)
        activity_text = ax_cum.text(
            0.98, 0.95, '', transform=ax_cum.transAxes,
            fontsize=9, verticalalignment='top', horizontalalignment='right',
            fontfamily='monospace',
            bbox=dict(boxstyle='round,pad=0.4', facecolor='lightyellow',
                      edgecolor='gray', alpha=0.9))

        def _format_activity(results):
            if not results:
                return "Activity: waiting..."
            lines = []
            for iso in results.get('isotopes', []):
                name = iso.get('isotope', '?')
                act = iso.get('activity_MBq', 0.0)
                sig = iso.get('sigma_activity_MBq', 0.0)
                valid = iso.get('valid', False)
                if valid:
                    lines.append("{}: {:.4f} \u00b1 {:.4f} MBq".format(
                        name, act, sig))
                else:
                    lines.append("{}: ---".format(name))
            total = results.get('total_activity_MBq', 0.0)
            lines.append("\u2500" * 24)
            lines.append("Total: {:.4f} MBq".format(total))
            window = results.get('live_time_s', 0.0)
            lines.append("Window: {:.1f}s".format(window))
            return "\n".join(lines)

        # Log scale toggle state
        self.log_scale = [False]

        def on_key(event):
            if event.key == 'l':
                self.log_scale[0] = not self.log_scale[0]
                scale = 'log' if self.log_scale[0] else 'linear'
                ax_cum.set_yscale(scale)
                ax_snap.set_yscale(scale)
                if self.log_scale[0]:
                    ax_cum.set_ylim(0.5, None)
                    ax_snap.set_ylim(0.5, None)
                fig.canvas.draw_idle()
            elif event.key == 'c':
                # Clear the DISPLAY accumulation only (client-side). Does not touch
                # the detector or any recording - the spectrum topic is per-interval deltas.
                with self.lock:
                    self.counts_cumulative[:] = 0
                    self.new_data = True
                with self.singles_lock:
                    self.counts_singles[:] = 0
                    self.new_singles_data = True
                print("Display spectrum cleared (c).")

        fig.canvas.mpl_connect('key_press_event', on_key)

        def update(frame):
            needs_redraw = False

            with self.lock:
                if self.new_data:
                    data_cum = self.counts_cumulative.copy()
                    self.new_data = False
                    needs_redraw = True
                else:
                    data_cum = None

            with self.singles_lock:
                if self.new_singles_data:
                    data_singles = self.counts_singles.copy()
                    self.new_singles_data = False
                    needs_redraw = True
                else:
                    data_singles = None

            if not needs_redraw:
                with self.activity_lock:
                    activity_text.set_text(_format_activity(self.activity_results))
                return list(bars_cum) + list(bars_snap)

            # Update cumulative bars
            if data_cum is not None:
                for bar, h in zip(bars_cum, data_cum):
                    bar.set_height(h)
                ymax_cum = data_cum.max() * 1.1 if data_cum.max() > 0 else 10
                if self.log_scale[0]:
                    ax_cum.set_ylim(0.5, ymax_cum * 2)
                else:
                    ax_cum.set_ylim(0, ymax_cum)
                ax_cum.set_title("All Events Spectrum | Total: {} counts".format(
                    int(data_cum.sum())))

            # Update singles bars
            if data_singles is not None:
                for bar, h in zip(bars_snap, data_singles):
                    bar.set_height(h)
                ymax_snap = data_singles.max() * 1.1 if data_singles.max() > 0 else 10
                if self.log_scale[0]:
                    ax_snap.set_ylim(0.5, ymax_snap * 2)
                else:
                    ax_snap.set_ylim(0, ymax_snap)
                ax_snap.set_title("Singles-Only Spectrum | Total: {} counts".format(
                    int(data_singles.sum())))

            with self.activity_lock:
                activity_text.set_text(_format_activity(self.activity_results))

            return list(bars_cum) + list(bars_snap)

        ani = animation.FuncAnimation(fig, update, interval=250, blit=False)
        plt.tight_layout()
        plt.show()

    def stop(self):
        self.listener.stop()
        self.singles_listener.stop()
        self.activity_listener.stop()


def main():
    app = prism.Application("plot_live_spectrum", "Live GeGI spectrum plotter", sys.argv)
    app.add_string_option("PlotLiveSpectrum", "topic", "Spectrum topic", "gegi.spectrum.histogram")
    app.add_string_option("PlotLiveSpectrum", "cal", "Path to EnergyCal.csv", "")
    app.add_bool_option("PlotLiveSpectrum", "cumulative",
                         "Accumulate counts over time (default: show latest snapshot)")

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    topic = result.get_string("topic")
    cal = result.get_string("cal")
    cumulative = result.get_bool("cumulative")

    if not cal:
        default = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "config", "EnergyCal.csv")
        if os.path.exists(default):
            cal = default

    connection = app.create_connection(result)
    if connection is None:
        print("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    plotter = LiveSpectrumPlotter(app, result, connection, topic, cal, cumulative)
    plotter.run()
    plotter.stop()
    connection.close()


if __name__ == "__main__":
    main()
