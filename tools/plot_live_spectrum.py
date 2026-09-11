#!/usr/bin/env python
"""
Live Spectrum Plotter - Runs natively on Windows (or any machine) via Prism.

Connects directly to the Prism messaging backend (NATS by default, same as
the C++ driver and the Python processing nodes) and subscribes to the
spectrum, singles-spectrum, and activity-results topics. Displays a
real-time matplotlib energy histogram, with optional live peak labels.

Prerequisites:
  - The Prism Python bindings must be built and installed (see project
    README) -- this replaces the old `pip install roslibpy` requirement.
  - matplotlib, numpy
  - Peak labels (optional): a nuclide_library.yaml (see --library, default
    config/nuclide_library.yaml). The plotter runs without labels if the
    library is missing or fails to load.

Usage:
  python plot_live_spectrum.py --protocol nats --server localhost --port 4222
  python plot_live_spectrum.py --cumulative --cal ../config/EnergyCal.csv
"""
import json
import os
import sys
import threading
import time

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "src", "phds_gegi_driver"))

import prism

import prism_messages as pmsg

# Isotope-ID screening engine (matched-filter peak search + library match):
# used to LABEL identified peaks on the live spectrum. Optional -- the
# plotter runs without labels if the module/library is unavailable.
try:
    import isotope_id
except ImportError:
    isotope_id = None


class LiveSpectrumPlotter(object):
    def __init__(self, app, result, connection, topic, cal_path, cumulative,
                 library_path=""):
        self.cumulative = cumulative
        self.bin_edges = self._load_cal(cal_path)
        self.n_bins = len(self.bin_edges)
        self.counts_cumulative = np.zeros(self.n_bins, dtype=np.float64)
        self.counts_singles = np.zeros(self.n_bins, dtype=np.float64)
        self.lock = threading.Lock()
        self.singles_lock = threading.Lock()
        self.new_data = False
        self.new_singles_data = False

        # Nuclide library for live peak labels (optional). Unlike the
        # upstream ROS driver's plotter, this always identifies peaks LOCALLY
        # from this tool's own display buffer, rather than preferring a
        # pipeline-published /identified_lines summary: this fork's
        # equivalent topic (gegi.data_recorder.identified, see
        # data_recorder_node.py) is a compact "Nuclide:score" text list for
        # heatmap gating, not the richer per-line energy/tags/persistence
        # payload the pipeline-preferred labeling path needs. Local
        # identification finds its own peak positions on the SAME buffer this
        # tool is already displaying, so it is effectively the same
        # information upstream's "pipeline quiet" fallback path used.
        self.library = None
        if isotope_id is not None and library_path and \
                os.path.exists(library_path):
            try:
                self.library = isotope_id.load_nuclide_library(library_path)
                print("Peak labels: %d-nuclide ID library loaded"
                      % len(self.library['nuclides']))
            except Exception as e:
                print("Peak labels disabled (library load failed: %s)" % e)
        self.peak_artists = []
        self._last_id_time = 0.0

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

    def _draw_labels(self, ax, data, entries):
        """(Re)draw peak labels: entries = [(energy_keV, text)]."""
        for art in self.peak_artists:
            try:
                art.remove()
            except ValueError:
                pass
        self.peak_artists = []
        ymax = max(float(data.max()), 1.0)
        for energy, name in sorted(entries):
            idx = int(np.searchsorted(self.bin_edges, energy)) - 1
            lo, hi = max(0, idx - 5), min(len(data), idx + 6)
            height = float(data[lo:hi].max()) if hi > lo else 0.0
            art = ax.annotate(
                "{} ({:.0f})".format(name, energy),
                xy=(energy, height), xytext=(energy, height + 0.03 * ymax),
                rotation=90, fontsize=8, ha='center', va='bottom',
                color='crimson',
                bbox=dict(boxstyle='round,pad=0.15', facecolor='white',
                          edgecolor='crimson', alpha=0.75))
            self.peak_artists.append(art)

    @staticmethod
    def _format_label(line):
        """Display text for one labelled line. Decorations:
          'Co-57?'   contested - an unidentified nuclide also fits the peak
          '~Eu-152'  unknown peak, but this nuclide WOULD fit (candidate hint)
          '*'        backscatter-suspect region
          'annih.'   unmatched 511 keV
        """
        text = line.get('label', '?')
        tags = line.get('tags', [])
        if text == '?':
            cand = next((t.split(':', 1)[1] for t in tags
                         if t.startswith('candidates:')), None)
            if cand:
                text = '~' + cand
            elif 'annihilation' in tags:
                text = 'annih.'
        if 'backscatter-suspect' in tags:
            text += '*'
        return text

    def _relabel_peaks(self, ax, data):
        """Identify peaks in the CURRENT display buffer and label them.

        Rebin toward ~0.8 keV so the matched filter is fast in the GUI loop.
        This mirrors the upstream ROS driver's plotter's fallback path (local
        identification), used here unconditionally -- see the __init__
        docstring note on why this fork does not have a richer pipeline
        /identified_lines-equivalent topic to prefer instead.
        """
        bin_w = float(np.median(np.diff(self.bin_edges))) or 1.0
        factor = max(1, int(round(0.8 / bin_w)))
        centers = self.bin_edges + bin_w / 2.0
        m = (len(data) // factor) * factor
        counts = data[:m].reshape(-1, factor).sum(axis=1).copy()
        cents = centers[:m].reshape(-1, factor).mean(axis=1)
        counts[-1] = 0.0     # overflow bin guard

        peaks, results, _ = isotope_id.identify(counts, cents, self.library)
        labeled = isotope_id.label_peaks(peaks, results)
        entries = [(l['energy_keV'], self._format_label(l)) for l in labeled]
        self._draw_labels(ax, data, entries)

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

                # Periodically (re)label peaks on the cumulative buffer.
                if self.library is not None and data_cum.sum() > 500 and \
                        time.time() - self._last_id_time > 10.0:
                    self._last_id_time = time.time()
                    try:
                        self._relabel_peaks(ax_cum, data_cum)
                    except Exception as e:
                        print("peak labelling failed: %s" % e)

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
    app.add_string_option("PlotLiveSpectrum", "library",
                          "nuclide_library.yaml for live peak labels (default: config/nuclide_library.yaml)", "")

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    topic = result.get_string("topic")
    cal = result.get_string("cal")
    cumulative = result.get_bool("cumulative")
    library = result.get_string("library")

    if not cal:
        default = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "config", "EnergyCal.csv")
        if os.path.exists(default):
            cal = default

    if not library:
        default_lib = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "config", "nuclide_library.yaml")
        if os.path.exists(default_lib):
            library = default_lib

    connection = app.create_connection(result)
    if connection is None:
        print("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    plotter = LiveSpectrumPlotter(app, result, connection, topic, cal, cumulative,
                                  library_path=library)
    plotter.run()
    plotter.stop()
    connection.close()


if __name__ == "__main__":
    main()
