#!/usr/bin/env python
"""
Spherical Heatmap Node - Projects Compton cone scores onto a sphere.

Maps back-projection scores onto a sphere of configurable radius centered at
the detector origin. Publishes a binary point-cloud blob (colored by score)
plus a small JSON metadata header, and a source-direction JSON message for
the peak direction.

This avoids depth ambiguity by only estimating source direction.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import logging
import struct
import threading
import time
from collections import deque

import numpy as np
import prism
import yaml

import prism_messages as pmsg
from prism_command_channel import CommandServer

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("spherical_heatmap_node")

# Fixed topic names (not CLI-overridable; see prism_messages.py for schemas).
COMPTON_EVENT_TOPIC = "gegi.driver.compton_event"
CLOUD_TOPIC = "gegi.heatmap.cloud"
CLOUD_META_TOPIC = "gegi.heatmap.cloud_meta"
SOURCE_DIRECTION_TOPIC = "gegi.heatmap.source_direction"
SOURCE_DIRECTIONS_TOPIC = "gegi.heatmap.source_directions"
SOURCE_ISOTOPES_TOPIC = "gegi.heatmap.source_isotopes"
ACTIVITY_RESULTS_TOPIC = "gegi.activity.results"
EFFECTIVE_SOURCE_DISTANCE_TOPIC = "gegi.activity.effective_source_distance"
# Data recorder's isotope-ID screening summary (see data_recorder_node.py's
# IDENTIFIED_TOPIC docstring for the wire format: compact 'Name:score|...'
# text, or 'none').
IDENTIFIED_TOPIC = "gegi.data_recorder.identified"

# Isotope-ID screening layer (matched-filter peak search + nuclide library
# match). Optional: only used to load the nuclide library for identified_bands().
try:
    import isotope_id
except ImportError:
    isotope_id = None

# Known isotope photo-peak energies (keV) and identification windows
ISOTOPE_PEAKS = {
    'Cs-137': {'energy': 662, 'window': 80},
    'Co-60':  {'energy': 1252, 'window': 200},  # avg of 1173+1332, wide window covers both
}

# Specific gamma-ray dose-rate constants (uSv*m^2 / MBq*h). The authoritative
# values live in config/isotopes.yaml under `gamma_constants`; this dict is only
# the fallback used when that file is missing or lacks the section.
DEFAULT_GAMMA_CONSTANTS = {
    'Cs-137': 0.0771,
    'Co-60':  0.3059,
}


def _norm_iso(name):
    """Normalise an isotope name for lookup: uppercase, drop punctuation.
    So 'Cs137', 'Cs-137', 'cs_137' and 'Co60_1173' all collapse sensibly."""
    return ''.join(ch for ch in str(name).upper() if ch.isalnum())


def parse_identified_msg(msg_text):
    """Parse the data recorder's identified-lines summary into nuclide names.

    msg_text: compact '|'-separated 'Name:score' text (see
    data_recorder_node.pub_identified), e.g. 'Eu-152:0.83|Cs-137:1.00'.
    'none' or '' (nothing identified) -> []. Pure function (unit-tested).
    """
    if not msg_text or msg_text == 'none':
        return []
    names = []
    for part in msg_text.split('|'):
        part = part.strip()
        if not part:
            continue
        name = part.split(':', 1)[0].strip()
        if name:
            names.append(name)
    return names


def identified_bands(names, nuclide_library, window_kev=30.0):
    """Compton-imaging energy bands for the default isotopes PLUS any
    additional nuclide the isotope-ID screening layer has identified.

    names: nuclide names from parse_identified_msg (data recorder's screening
    pass). nuclide_library: {name: {representative_keV, imaging_keV,
    imaging_window_kev, ...}} (isotope_id.load_nuclide_library()['nuclides']).

    The DEFAULT isotopes (ISOTOPE_PEAKS: Cs-137, Co-60) always keep their
    hand-tuned band, even when also identified by screening - narrowing the
    Co-60 band would starve the imaging statistics and worsen the hotspot
    bias. A newly identified nuclide gets EITHER its library 'imaging_keV'/
    'imaging_window_kev' override (a line CLUSTER the nuclide is better
    imaged on, e.g. Eu-152's 964+1086+1112 keV group) or a window_kev-wide
    band centred on its 'representative_keV'. Unknown names (not in the
    library) are ignored. Pure function (unit-tested).
    """
    bands = dict(ISOTOPE_PEAKS)
    for name in names:
        if name in bands:
            continue
        nuc = nuclide_library.get(name)
        if not nuc:
            continue
        if 'imaging_keV' in nuc:
            energy = float(nuc['imaging_keV'])
            window = float(nuc.get('imaging_window_kev', window_kev))
        else:
            rep = nuc.get('representative_keV')
            if rep is None:
                continue
            energy = float(rep)
            window = window_kev
        bands[name] = {'energy': energy, 'window': window}
    return bands


def load_gamma_constants(config_path):
    """Load specific gamma-ray dose-rate constants from isotopes.yaml.

    Returns a dict keyed by the normalised isotope name so lookups work whether
    the caller passes the yaml form ('Cs137') or the display form ('Cs-137').
    Falls back to DEFAULT_GAMMA_CONSTANTS if the file is missing/unreadable.
    """
    table = {_norm_iso(k): float(v) for k, v in DEFAULT_GAMMA_CONSTANTS.items()}
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            for k, v in (cfg.get('gamma_constants', {}) or {}).items():
                table[_norm_iso(k)] = float(v)
        except Exception as exc:  # noqa: broad - never let dose weighting crash
            logger.warning("Could not load gamma_constants from %s: %s",
                            config_path, exc)
    return table


def gamma_constant_for(table, iso_name, default=0.077):
    """Look up a gamma constant tolerant of naming ('Co-60' vs 'Co60_1173')."""
    key = _norm_iso(iso_name)
    if key in table:
        return table[key]
    for k, v in table.items():
        if key.startswith(k) or k.startswith(key):
            return v
    return default


def identify_isotope(energies_kev):
    """Identify the best-matching isotope from event energies near a peak.
    Returns (isotope_name, confidence) or (None, 0) if no clear match."""
    if len(energies_kev) < 5:
        return None, 0.0
    best_name = None
    best_fraction = 0.0
    for name, info in ISOTOPE_PEAKS.items():
        center = info['energy']
        window = info['window']
        in_window = np.sum((energies_kev > center - window) & (energies_kev < center + window))
        fraction = in_window / float(len(energies_kev))
        if fraction > best_fraction:
            best_fraction = fraction
            best_name = name
    if best_fraction >= 0.25:
        return best_name, best_fraction
    return None, 0.0


def fibonacci_sphere(n_points):
    """Generate approximately uniform points on a unit sphere using Fibonacci spiral."""
    indices = np.arange(0, n_points, dtype=np.float64)
    phi = np.arccos(1 - 2.0 * (indices + 0.5) / n_points)
    theta = np.pi * (1 + np.sqrt(5)) * indices
    x = np.sin(phi) * np.cos(theta)
    y = np.sin(phi) * np.sin(theta)
    z = np.cos(phi)
    return np.stack([x, y, z], axis=1)


class SphericalHeatmapNode(object):
    def __init__(self, app, args, connection):
        self._app = app

        # Dose-rate gamma constants, single-sourced from isotopes.yaml.
        self.isotopes_config = args.get_string("isotopes-config")
        self.gamma_constants = load_gamma_constants(self.isotopes_config)

        # Nuclide library for identified_bands(): widens the imaging bands to
        # nuclides the data recorder's isotope-ID screening layer identifies,
        # beyond the default Cs-137/Co-60 (see identified_bands() docstring).
        nuclide_library_path = args.get_string("nuclide-library")
        self.nuclide_library = {}
        if nuclide_library_path and isotope_id is not None:
            try:
                self.nuclide_library = isotope_id.load_nuclide_library(
                    nuclide_library_path).get('nuclides', {})
            except Exception as exc:
                logger.warning("Could not load nuclide library %s: %s",
                               nuclide_library_path, exc)
        self._identified_names = []
        self._identified_lock = threading.Lock()

        self.radius = args.get_float("radius")  # 1m diameter
        self.n_points = int(args.get_int("n-points"))
        self.window_s = args.get_float("window-s")
        self.update_period_s = args.get_float("update-period-s")
        self.min_events = int(args.get_int("min-events"))
        # Cap on events fed to the live back-projection. Raised so the live image
        # uses far more of the stream (the raw jsonl already keeps every event).
        # Cost is O(max_events x n_points) per update; lower it if updates lag.
        self.max_events = int(args.get_int("max-events"))
        self.sigma_floor = args.get_float("sigma-floor")
        self.max_uncertainty = args.get_float("max-uncertainty")
        self.hemisphere_only = not args.get_bool("no-hemisphere-only")
        self.max_peaks = int(args.get_int("max-peaks"))
        self.peak_min_separation_deg = args.get_float("peak-min-separation-deg")
        # Fraction of the strongest peak a secondary source must reach to be
        # reported. Off-axis sources back-project weaker than on-axis ones (lower
        # detection efficiency), so a high gate (0.75) makes similar-activity
        # off-axis sources flicker in/out around the threshold. 0.5 keeps genuine
        # multi-source scenes stable while still rejecting noise ridges.
        self.peak_threshold = args.get_float("peak-threshold")  # fraction of max score
        self.refine_peaks = not args.get_bool("no-refine-peaks")
        # Temporal persistence: report a candidate peak only if a same-isotope
        # peak appeared within peak_persist_tol_deg in at least peak_persist_min
        # of the last peak_persist_frames frames (including the current one).
        # Stable real sources pass immediately; flickering ghost peaks from
        # Compton cone cross-talk are rejected. Set peak_persist_min <= 1 to
        # disable. Costs (peak_persist_min - 1) frames of latency for new sources.
        self.peak_persist_frames = int(args.get_int("peak-persist-frames"))
        self.peak_persist_min = int(args.get_int("peak-persist-min"))
        self.peak_persist_tol_deg = args.get_float("peak-persist-tol-deg")
        self._peak_history = deque(maxlen=max(1, self.peak_persist_frames))
        # Ghost suppression (default: support-based rejection). For each candidate
        # peak, count the events whose Compton cones actually pass through it. A
        # real source is on the cones of all its own events; a ghost sits only
        # on coincidental crossings, so its support is far lower. A peak
        # is kept only if its support >= max(min_source_events, support_ratio *
        # strongest peak's support). This uses no event removal / re-solving, so
        # it cannot create new artifacts. Raise support_ratio to reject more
        # ghosts; lower it to keep weaker real sources.
        self.support_ratio = args.get_float("support-ratio")
        self.min_source_events = int(args.get_int("min-source-events"))
        self.attribution_tol_deg = args.get_float("attribution-tol-deg")
        # Optional alternative: CLEAN-style iterative extraction (off by default;
        # can over-produce peaks with strong multi-source scenes).
        self.iterative_extraction = args.get_bool("iterative-extraction")

        node_name = args.get_string("node-name")

        # Build sphere grid
        all_pts = fibonacci_sphere(self.n_points * (1 if not self.hemisphere_only else 2))
        if self.hemisphere_only:
            # Keep only +X hemisphere (forward-looking)
            all_pts = all_pts[all_pts[:, 0] > 0]
        self.directions = all_pts / np.linalg.norm(all_pts, axis=1, keepdims=True)
        self.sphere_points = self.directions * self.radius
        logger.info("Spherical heatmap: %d points on %.1fm radius sphere",
                    self.sphere_points.shape[0], self.radius)

        self.events = deque()
        self.lock = threading.Lock()

        # CSV export settings
        self.csv_output_dir = args.get_string("csv-output-dir")
        self.csv_enabled = args.get_bool("csv-enabled")
        self.raster_cell_m = args.get_float("raster-cell-m")  # 5mm cells
        self.raster_fov_m = args.get_float("raster-fov-m")  # +/-0.5m coverage

        self._log_last_time = {}

        # -- Senders ----------------------------------------------------------
        cloud_bin_cfg = prism.BinarySenderConfig()
        cloud_bin_cfg.destination = CLOUD_TOPIC
        self.pub_cloud = app.create_binary_sender(args, connection, cloud_bin_cfg)

        cloud_meta_cfg = prism.TextSenderConfig()
        cloud_meta_cfg.destination = CLOUD_META_TOPIC
        self.pub_cloud_meta = app.create_text_sender(args, connection, cloud_meta_cfg)

        direction_cfg = prism.TextSenderConfig()
        direction_cfg.destination = SOURCE_DIRECTION_TOPIC
        self.pub_direction = app.create_text_sender(args, connection, direction_cfg)

        directions_cfg = prism.TextSenderConfig()
        directions_cfg.destination = SOURCE_DIRECTIONS_TOPIC
        self.pub_directions = app.create_text_sender(args, connection, directions_cfg)

        isotopes_cfg = prism.TextSenderConfig()
        isotopes_cfg.destination = SOURCE_ISOTOPES_TOPIC
        self.pub_isotopes = app.create_text_sender(args, connection, isotopes_cfg)

        # -- Receivers ----------------------------------------------------------
        compton_cfg = prism.TextReceiverConfig()
        compton_cfg.source = COMPTON_EVENT_TOPIC
        self.sub = app.create_text_receiver(args, connection, compton_cfg)
        self.sub.on_receive(self.on_event)
        self.sub.start()

        # Subscribe to activity results for absolute dose rate scaling
        self._dose_rate_uSv_h = {}  # isotope display name -> dose rate in uSv/h
        activity_cfg = prism.TextReceiverConfig()
        activity_cfg.source = ACTIVITY_RESULTS_TOPIC
        self.sub_activity = app.create_text_receiver(args, connection, activity_cfg)
        self.sub_activity.on_receive(self._on_activity)
        self.sub_activity.start()

        # Track the plate-derived source distance so the imaging sphere and dose
        # scaling follow the shielding standoff set on the activity node.
        distance_cfg = prism.TextReceiverConfig()
        distance_cfg.source = EFFECTIVE_SOURCE_DISTANCE_TOPIC
        self.sub_distance = app.create_text_receiver(args, connection, distance_cfg)
        self.sub_distance.on_receive(self._on_source_distance)
        self.sub_distance.start()

        # Isotope-ID screening summary from the data recorder, widening the
        # imaging bands beyond the default Cs-137/Co-60 (see identified_bands()).
        identified_cfg = prism.TextReceiverConfig()
        identified_cfg.source = IDENTIFIED_TOPIC
        self.sub_identified = app.create_text_receiver(args, connection, identified_cfg)
        self.sub_identified.on_receive(self._on_identified)
        self.sub_identified.start()

        # -- Command channel (replaces the old ~clear / ~save_csv Trigger services)
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
        self.command_server.on("save_csv", self._handle_save_csv)
        self.command_server.start()

        # -- Periodic update thread (replaces rospy.Timer) ---------------------
        self._stop_event = threading.Event()
        self._timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self._timer_thread.start()

    def _throttled_log(self, key, period_s, log_fn, msg, *args):
        now = time.time()
        last = self._log_last_time.get(key, 0.0)
        if now - last >= period_s:
            self._log_last_time[key] = now
            log_fn(msg, *args)

    def _timer_loop(self):
        period = max(self.update_period_s, 0.01)
        while self._app.is_running() and not self._stop_event.is_set():
            time.sleep(period)
            if not self._app.is_running() or self._stop_event.is_set():
                break
            try:
                self.on_timer()
            except Exception as e:
                logger.error("on_timer failed: %s", e)

    def stop(self):
        self._stop_event.set()
        self._timer_thread.join(timeout=2.0)
        self.command_server.stop()
        self.sub.stop()
        self.sub_activity.stop()
        self.sub_distance.stop()
        self.sub_identified.stop()

    def _on_identified(self, message, source=None):
        """Cache the data recorder's latest isotope-ID screening names."""
        names = parse_identified_msg(message)
        with self._identified_lock:
            self._identified_names = names

    def _on_source_distance(self, message, source=None):
        """Update sphere radius / dose standoff from the plate-derived distance."""
        try:
            payload = json.loads(message)
        except Exception:
            return
        d = pmsg.parse_double_value(payload)
        if d is None:
            return
        d = float(d)
        if d > 0 and abs(d - self.radius) > 1e-4:
            with self.lock:
                self.radius = d
                self.sphere_points = self.directions * self.radius
            logger.info("Sphere heatmap: source distance -> %.3fm", d)

    def _handle_clear(self, params):
        with self.lock:
            n = len(self.events)
            self.events.clear()
        logger.info("Cleared %d events from sphere heatmap buffer", n)
        return True, "Cleared {} events".format(n)

    def _on_activity(self, message, source=None):
        """Parse activity results and compute dose rate per isotope."""
        try:
            data = json.loads(message)
            d = self.radius  # source distance = sphere radius
            dose_rates = {}
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
                # For Co-60 take max of two peaks (same source activity)
                if display in activities:
                    activities[display] = max(activities[display], activity_mbq)
                else:
                    activities[display] = activity_mbq
            for display, a_mbq in activities.items():
                gamma = gamma_constant_for(self.gamma_constants, display)
                dose_rates[display] = gamma * a_mbq / (d * d) if d > 0 else 0.0
            with self.lock:
                self._dose_rate_uSv_h = dose_rates
        except (ValueError, TypeError):
            pass

    def on_event(self, message, source=None):
        try:
            payload = json.loads(message)
            evt = pmsg.parse_compton_event(payload)
        except Exception:
            return

        if evt['cone_angle_uncertainty'] <= 0.0 or evt['cone_angle_uncertainty'] > self.max_uncertainty:
            return

        p1 = np.array(evt['reading_location_1'], dtype=np.float64)
        p2 = np.array(evt['reading_location_2'], dtype=np.float64)

        axis = p1 - p2
        n = np.linalg.norm(axis)
        if n < 1e-9:
            return
        axis = axis / n
        total_energy = evt['energy_kev_1'] + evt['energy_kev_2']

        with self.lock:
            self.events.append((pmsg.now_seconds(), p1, axis, float(evt['cone_angle']), total_energy))

    def _prune(self, now):
        cutoff = now - self.window_s
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        if self.max_events > 0 and len(self.events) > self.max_events:
            for _ in range(len(self.events) - self.max_events):
                self.events.popleft()

    def _solve(self, events):
        points = self.sphere_points
        scores = np.zeros(points.shape[0], dtype=np.float64)

        for _, apex, axis, angle, _energy in events:
            ap = points - apex
            ap_norm = np.linalg.norm(ap, axis=1)
            cos_actual = np.dot(ap, axis) / np.maximum(ap_norm, 1e-9)
            cos_actual = np.clip(cos_actual, -1.0, 1.0)
            actual_angle = np.arccos(cos_actual)
            ang_dev = np.abs(actual_angle - angle)
            scores += np.exp(-0.5 * (ang_dev / self.sigma_floor) ** 2)

        return scores

    def _solve_events(self, band_events):
        """Back-project a pre-filtered list of (apex, axis, angle) tuples."""
        points = self.sphere_points
        scores = np.zeros(points.shape[0], dtype=np.float64)
        for apex, axis, angle in band_events:
            ap = points - apex
            ap_norm = np.linalg.norm(ap, axis=1)
            cos_actual = np.dot(ap, axis) / np.maximum(ap_norm, 1e-9)
            cos_actual = np.clip(cos_actual, -1.0, 1.0)
            actual_angle = np.arccos(cos_actual)
            ang_dev = np.abs(actual_angle - angle)
            scores += np.exp(-0.5 * (ang_dev / self.sigma_floor) ** 2)
        return scores

    def _solve_energy_filtered(self, events, energy_center, energy_window):
        """Solve using only events within a specific energy band."""
        points = self.sphere_points
        scores = np.zeros(points.shape[0], dtype=np.float64)
        count = 0

        for _, apex, axis, angle, energy in events:
            if not (energy_center - energy_window < energy < energy_center + energy_window):
                continue
            ap = points - apex
            ap_norm = np.linalg.norm(ap, axis=1)
            cos_actual = np.dot(ap, axis) / np.maximum(ap_norm, 1e-9)
            cos_actual = np.clip(cos_actual, -1.0, 1.0)
            actual_angle = np.arccos(cos_actual)
            ang_dev = np.abs(actual_angle - angle)
            scores += np.exp(-0.5 * (ang_dev / self.sigma_floor) ** 2)
            count += 1

        return scores, count

    def _events_near_direction(self, events, direction, cone_half_angle_rad=0.10):
        """Return energies of events whose cones pass near the given direction.
        Uses tighter cone matching to better attribute events to this peak."""
        energies = []
        for _, apex, axis, angle, energy in events:
            # Check if this event's Compton cone intersects the direction
            vec = self.radius * direction - apex
            vec_norm = np.linalg.norm(vec)
            if vec_norm < 1e-9:
                continue
            cos_actual = np.dot(vec / vec_norm, axis)
            cos_actual = np.clip(cos_actual, -1.0, 1.0)
            actual_angle = np.arccos(cos_actual)
            if abs(actual_angle - angle) < cone_half_angle_rad:
                energies.append(energy)
        return np.array(energies)

    def _identify_peak_isotope(self, events, direction):
        """Score each isotope's events separately at this direction.
        Uses average score per event to avoid bias from source activity differences."""
        scores_by_isotope = {}
        for name, info in ISOTOPE_PEAKS.items():
            center = info['energy']
            window = info['window']
            score_sum = 0.0
            count = 0
            for _, apex, axis, angle, energy in events:
                total_e = energy
                if not (center - window < total_e < center + window):
                    continue
                vec = self.radius * direction - apex
                vec_norm = np.linalg.norm(vec)
                if vec_norm < 1e-9:
                    continue
                cos_actual = np.dot(vec / vec_norm, axis)
                cos_actual = np.clip(cos_actual, -1.0, 1.0)
                actual_angle = np.arccos(cos_actual)
                ang_dev = abs(actual_angle - angle)
                score_sum += np.exp(-0.5 * (ang_dev / self.sigma_floor) ** 2)
                count += 1
            avg_score = score_sum / max(count, 1)
            scores_by_isotope[name] = (avg_score, count)

        # Pick the isotope whose events fit best (highest average score) at this direction
        best_name = None
        best_avg = 0.0
        for name, (avg, count) in scores_by_isotope.items():
            if count >= 3 and avg > best_avg:
                best_avg = avg
                best_name = name
        if best_name is None:
            return None, 0.0
        total_avg = sum(a for a, _ in scores_by_isotope.values())
        confidence = best_avg / max(total_avg, 1e-9)
        return best_name, confidence

    def _is_local_maximum(self, idx, scores, radius_rad=0.18):
        """Check if point idx is a local maximum within the given angular radius.
        
        radius_rad=0.18 (~10.3 deg) ensures only dominant peaks survive,
        rejecting Compton ring sidelobes which are typically narrower.
        """
        center_dir = self.directions[idx]
        cos_angles = self.directions.dot(center_dir)
        neighbors = np.where((cos_angles > np.cos(radius_rad)) & (cos_angles < 1.0 - 1e-9))[0]
        if len(neighbors) == 0:
            return True
        return scores[idx] >= scores[neighbors].max()

    def _find_peaks(self, scores):
        """Find multiple local maxima on the sphere with minimum angular separation."""
        min_sep_rad = np.radians(self.peak_min_separation_deg)
        max_score = scores.max()
        min_score = scores.min()
        score_range = max_score - min_score
        if score_range < 1e-12:
            return []

        sorted_indices = np.argsort(scores)[::-1]
        peaks = []

        for idx in sorted_indices:
            if len(peaks) >= self.max_peaks:
                break

            # Must be a local maximum (filters out Compton ring ridge points)
            if not self._is_local_maximum(int(idx), scores):
                continue

            # Secondary peaks must score at least threshold * primary.
            # Use continue (not break) so one sub-threshold candidate does not
            # abort the search for other valid, well-separated peaks.
            if peaks:
                primary_norm = (scores[peaks[0]] - min_score) / score_range
                this_norm = (scores[idx] - min_score) / score_range
                if this_norm < self.peak_threshold * primary_norm:
                    continue

            # Check angular separation from existing peaks
            too_close = False
            for peak_idx in peaks:
                cos_sep = np.dot(self.directions[idx], self.directions[peak_idx])
                cos_sep = np.clip(cos_sep, -1.0, 1.0)
                sep = np.arccos(cos_sep)
                if sep < min_sep_rad:
                    too_close = True
                    break

            if not too_close:
                peaks.append(int(idx))

        return peaks

    def _events_on_direction_mask(self, band_events, direction, tol_rad):
        """Boolean mask of events whose Compton cone passes within tol_rad of direction."""
        target = self.radius * direction
        mask = np.zeros(len(band_events), dtype=bool)
        for i, (apex, axis, angle) in enumerate(band_events):
            vec = target - apex
            vn = np.linalg.norm(vec)
            if vn < 1e-9:
                continue
            cos_actual = np.clip(np.dot(vec / vn, axis), -1.0, 1.0)
            if abs(np.arccos(cos_actual) - angle) < tol_rad:
                mask[i] = True
        return mask

    def _filter_peaks_by_support(self, peak_indices, scores, band_events):
        """Reject cone-crossing ghost peaks by event support, then refine.

        A real source lies on the cones of all its own events; a ghost sits only
        on coincidental crossings from other sources, so its support is far lower.
        Keep a peak if its support >= max(min_source_events, support_ratio *
        strongest peak's support). Returns refined positions of the kept peaks.
        """
        if not peak_indices:
            return []
        attr_tol = np.radians(self.attribution_tol_deg)
        supports = []
        for pidx in peak_indices:
            mask = self._events_on_direction_mask(
                band_events, self.directions[pidx], attr_tol)
            supports.append(int(np.count_nonzero(mask)))
        max_support = max(supports) if supports else 0
        floor = max(self.min_source_events, int(self.support_ratio * max_support))
        positions = []
        for pidx, sup in zip(peak_indices, supports):
            if sup >= floor:
                positions.append(
                    self._refine_peak_centroid(pidx, scores)
                    if self.refine_peaks else self.sphere_points[pidx])
        return positions

    def _extract_peaks_iterative(self, events, energy_center, energy_window):
        """CLEAN-style iterative source extraction for one isotope band.

        Repeatedly: back-project the remaining events, take the strongest
        well-separated local maximum, and if enough events' cones actually pass
        through it (>= min_source_events), record it and remove those events.
        Removing a real source's events collapses ghost peaks built from them.

        Returns (list_of_refined_positions, band_event_count).
        """
        band = [(apex, axis, angle)
                for (_, apex, axis, angle, energy) in events
                if energy_center - energy_window < energy < energy_center + energy_window]
        band_count = len(band)
        if band_count < self.min_source_events:
            return [], band_count

        min_sep_rad = np.radians(self.peak_min_separation_deg)
        attr_tol = np.radians(self.attribution_tol_deg)
        remaining = band
        found_positions = []
        found_idx = []

        for _ in range(self.max_peaks):
            if len(remaining) < self.min_source_events:
                break
            scores = self._solve_events(remaining)
            if scores.max() <= 0.0:
                break

            # Strongest local maximum not too close to an already-found source.
            cand_idx = None
            for idx in np.argsort(scores)[::-1]:
                idx = int(idx)
                if not self._is_local_maximum(idx, scores):
                    continue
                too_close = False
                for p in found_idx:
                    cs = np.clip(np.dot(self.directions[idx], self.directions[p]), -1.0, 1.0)
                    if np.arccos(cs) < min_sep_rad:
                        too_close = True
                        break
                if not too_close:
                    cand_idx = idx
                    break
            if cand_idx is None:
                break

            mask = self._events_on_direction_mask(
                remaining, self.directions[cand_idx], attr_tol)
            support = int(np.count_nonzero(mask))
            if support < self.min_source_events:
                break  # residual peak is a ghost/noise, not a real source

            if self.refine_peaks:
                refined_pos = self._refine_peak_centroid(cand_idx, scores)
            else:
                refined_pos = self.sphere_points[cand_idx]
            found_positions.append(np.asarray(refined_pos, dtype=np.float64))
            found_idx.append(cand_idx)

            remaining = [e for e, m in zip(remaining, mask) if not m]

        return found_positions, band_count

    def _refine_peak_centroid(self, peak_idx, scores, radius_rad=0.04):
        """Refine peak location using score-weighted centroid of nearby points.

        Instead of just taking the grid point with the highest score, compute
        a weighted average direction using all points within radius_rad.
        Uses a tight radius (0.04 rad ~ 2.3 deg) and high weighting exponent
        to avoid bias from neighboring sources.
        """
        center_dir = self.directions[peak_idx]
        cos_angles = self.directions.dot(center_dir)
        neighbors = np.where(cos_angles > np.cos(radius_rad))[0]

        if len(neighbors) < 3:
            return self.sphere_points[peak_idx]

        # Use scores raised to a power to sharpen the weighting
        neighbor_scores = scores[neighbors]
        # Shift so minimum in neighborhood is 0
        shifted = neighbor_scores - neighbor_scores.min()
        max_shifted = shifted.max()
        if max_shifted < 1e-12:
            return self.sphere_points[peak_idx]

        # Cube the weights for very sharp centroid (minimize pull from neighbors)
        weights = (shifted / max_shifted) ** 3

        # Weighted average of unit direction vectors
        weighted_dirs = self.directions[neighbors] * weights[:, np.newaxis]
        centroid_dir = weighted_dirs.sum(axis=0)
        norm = np.linalg.norm(centroid_dir)
        if norm < 1e-9:
            return self.sphere_points[peak_idx]

        centroid_dir /= norm
        return centroid_dir * self.radius

    def _apply_peak_persistence(self, candidates):
        """Reject flickering ghost peaks via temporal persistence.

        Keep a candidate only if a same-isotope peak appeared within
        peak_persist_tol_deg in at least peak_persist_min of the last
        peak_persist_frames frames (including the current one). This frame's raw
        candidates are always recorded for future frames, whether or not they
        are confirmed now.
        """
        current = [{'iso': c['iso'], 'unit': c['unit']} for c in candidates]

        if self.peak_persist_min <= 1:
            self._peak_history.append(current)
            return candidates

        cos_tol = np.cos(np.radians(self.peak_persist_tol_deg))
        history = list(self._peak_history)  # previous frames only

        confirmed = []
        for c in candidates:
            matches = 1  # current frame counts
            for frame in history:
                for h in frame:
                    if h['iso'] == c['iso'] and float(np.dot(h['unit'], c['unit'])) >= cos_tol:
                        matches += 1
                        break
            if matches >= self.peak_persist_min:
                confirmed.append(c)

        self._peak_history.append(current)
        return confirmed

    def on_timer(self):
        now = pmsg.now_seconds()
        with self.lock:
            self._prune(now)
            events = list(self.events)

        if len(events) < self.min_events:
            self._throttled_log("waiting_events", 10.0, logger.info,
                                 "sphere heatmap waiting: %d/%d events",
                                 len(events), self.min_events)
            return

        scores = self._solve(events)

        # Normalize scores to [0, 1] for visualization
        smin = scores.min()
        smax = scores.max()
        if smax - smin < 1e-12:
            norm_scores = np.zeros_like(scores)
        else:
            norm_scores = (scores - smin) / (smax - smin)

        # Publish peak direction (strongest)
        idx_peak = int(np.argmax(scores))

        direction_msg = {
            "stamp": now,
            "frame_id": "detector",
            "x": float(self.sphere_points[idx_peak, 0]),
            "y": float(self.sphere_points[idx_peak, 1]),
            "z": float(self.sphere_points[idx_peak, 2]),
        }
        self.pub_direction.send(json.dumps(direction_msg))

        # Find peaks per isotope energy band (default Cs-137/Co-60 plus any
        # nuclide the data recorder's isotope-ID screening layer has identified
        # - see identified_bands()). Collect raw candidates first, then apply
        # temporal persistence to drop flickering ghost peaks before publish.
        with self._identified_lock:
            identified_names = list(self._identified_names)
        bands = identified_bands(identified_names, self.nuclide_library)

        raw_candidates = []
        for iso_name, iso_info in bands.items():
            # Band-filter this isotope's events once.
            e_lo = iso_info['energy'] - iso_info['window']
            e_hi = iso_info['energy'] + iso_info['window']
            band = [(apex, axis, angle)
                    for (_, apex, axis, angle, energy) in events
                    if e_lo < energy < e_hi]
            iso_count = len(band)
            if iso_count < 5:
                continue

            if self.iterative_extraction:
                positions, _ = self._extract_peaks_iterative(
                    events, iso_info['energy'], iso_info['window'])
            else:
                iso_scores = self._solve_events(band)
                positions = self._filter_peaks_by_support(
                    self._find_peaks(iso_scores), iso_scores, band)
            if not positions:
                continue
            # ALL detected peaks for this isotope (supports multiple same-isotope sources)
            for refined_pos in positions:
                refined_pos = np.asarray(refined_pos, dtype=np.float64)
                pos_norm = np.linalg.norm(refined_pos)
                unit = refined_pos / pos_norm if pos_norm > 1e-9 else refined_pos
                # Convert sphere position to real-world coordinate at source plane
                # Y_real = distance * Y_sphere / X_sphere (ray-plane intersection)
                x_s = float(refined_pos[0])
                y_s = float(refined_pos[1])
                z_s = float(refined_pos[2])
                if abs(x_s) > 1e-6:
                    y_real = self.radius * y_s / x_s
                    z_real = self.radius * z_s / x_s
                else:
                    y_real = y_s
                    z_real = z_s
                raw_candidates.append({
                    'iso': iso_name, 'count': iso_count,
                    'unit': unit, 'y': y_real, 'z': z_real})

        confirmed_peaks = self._apply_peak_persistence(raw_candidates)

        isotope_labels = []
        points_msg = []
        for c in confirmed_peaks:
            points_msg.append({
                "x": self.radius,  # source distance along X
                "y": c['y'],
                "z": c['z'],
                "isotope": c['iso'],
                "count": c['count'],
            })
            isotope_labels.append("{}:{}".format(c['iso'], c['count']))

        directions_msg = {
            "stamp": now,
            "frame_id": "detector",
            "points": points_msg,
        }
        self.pub_directions.send(json.dumps(directions_msg))

        # Store peaks for CSV export (Y,Z in sphere coordinates + isotope name)
        self._last_peaks = [(p['y'], p['z']) for p in points_msg]
        self._last_isotope_labels = isotope_labels

        # Publish isotope identification with counts
        iso_text = "|".join(isotope_labels) if isotope_labels else "none"
        self.pub_isotopes.send(iso_text)

        # Publish point cloud with per-isotope scores scaled to dose rate (uSv/h)
        with self.lock:
            dose_rates = dict(self._dose_rate_uSv_h)

        iso_norm_scores = {}
        for iso_name, iso_info in bands.items():
            iso_scores, iso_count = self._solve_energy_filtered(
                events, iso_info['energy'], iso_info['window'])
            if iso_count >= 5:
                iso_min = iso_scores.min()
                iso_max = iso_scores.max()
                if iso_max - iso_min > 1e-12:
                    normed = (iso_scores - iso_min) / (iso_max - iso_min)
                else:
                    normed = np.zeros_like(iso_scores)
                # Scale peak to actual dose rate (uSv/h) from activity measurement
                dr = dose_rates.get(iso_name, 0.0)
                if dr > 0:
                    iso_norm_scores[iso_name] = normed * dr
                else:
                    # Fallback: use gamma constant as relative weight
                    gamma = gamma_constant_for(self.gamma_constants, iso_name)
                    iso_norm_scores[iso_name] = normed * gamma
            else:
                iso_norm_scores[iso_name] = np.zeros_like(scores)

        # Combine per-isotope scores (take max at each point)
        # Do NOT renormalize - values represent dose rate in uSv/h
        combined = np.zeros_like(scores)
        for v in iso_norm_scores.values():
            combined = np.maximum(combined, v)
        norm_scores = combined

        # Store for CSV export (command call or auto-save)
        self._last_iso_scores = iso_norm_scores
        self._last_combined = combined

        self._publish_cloud(now, norm_scores, iso_norm_scores)

        # Auto-save CSV on every update cycle
        if self.csv_enabled:
            try:
                self._save_csv(iso_norm_scores, combined)
            except Exception as e:
                self._throttled_log("csv_save_failed", 10.0, logger.error,
                                     "CSV save failed: %s", e)

        self._throttled_log(
            "status", 5.0, logger.info,
            "sphere heatmap: %d events, %d sources [%s], peak dose=%.3f uSv/h, dose_rates=%s",
            len(events), len(points_msg), iso_text,
            float(norm_scores.max()), dose_rates)

    def _handle_save_csv(self, params):
        """Command handler to trigger a one-shot CSV save."""
        if hasattr(self, '_last_iso_scores') and hasattr(self, '_last_combined'):
            self._save_csv(self._last_iso_scores, self._last_combined)
            return True, "CSV saved to " + self.csv_output_dir
        return False, "No heatmap data available yet"

    def _save_csv(self, iso_norm_scores, combined_scores):
        """Save both raw and rasterised CSV files."""
        self._last_iso_scores = iso_norm_scores
        self._last_combined = combined_scores

        try:
            if not os.path.exists(self.csv_output_dir):
                os.makedirs(self.csv_output_dir)
        except OSError:
            pass

        points = self.sphere_points
        n = points.shape[0]
        cs137 = iso_norm_scores.get('Cs-137', np.zeros(n))
        co60 = iso_norm_scores.get('Co-60', np.zeros(n))

        # --- Raw points CSV ---
        raw_path = os.path.join(self.csv_output_dir, "gegi_heatmap_raw.csv")
        with open(raw_path, 'w') as f:
            f.write("x,y,z,intensity\n")
            for i in range(n):
                f.write("{:.5f},{:.5f},{:.5f},{:.6f}\n".format(
                    points[i, 0], points[i, 1], points[i, 2],
                    combined_scores[i]))

        # --- Rasterised grid CSV ---
        peaks = getattr(self, '_last_peaks', [])
        peak_labels = getattr(self, '_last_isotope_labels', [])
        self._save_rasterised_csv(points, combined_scores, cs137, co60, peaks, peak_labels)

    @staticmethod
    def _gaussian_smooth(grid, sigma=3.0):
        """2D Gaussian smoothing - same as live 2D heatmap."""
        from scipy.ndimage import gaussian_filter
        return gaussian_filter(grid, sigma=sigma)

    def _save_rasterised_csv(self, points, combined, cs137, co60, peaks=None, peak_labels=None):
        """Rasterise sphere heatmap to CSV using the same algorithm as the live 2D heatmap.

        Pipeline:
        1. Bin per-isotope scores into Y-Z grid using direct sphere coordinates
        2. scipy gaussian_filter(sigma=5.0) - higher than live heatmap to compensate
           for matplotlib's bilinear interpolation which the CSV doesn't have
        3. Cosine correction + zero outside sphere
        4. Per-peak Gaussian blob masking (sigma=0.04m) for source separation
        """
        from scipy.ndimage import gaussian_filter

        res = 200  # 200x200 grid
        extent = 0.2  # +/-200mm (400mm x 400mm)

        y_edges = np.linspace(-extent, extent, res + 1)
        z_edges = np.linspace(-extent, extent, res + 1)
        yc = np.linspace(-extent, extent, res)
        zc = np.linspace(-extent, extent, res)
        YY, ZZ = np.meshgrid(yc, zc)
        r2 = YY**2 + ZZ**2
        R2 = self.radius**2

        def _project_to_2d_grid(scores):
            """Bin scores onto Y-Z grid with heavy smoothing to eliminate sampling artifacts."""
            grid = np.zeros((res, res), dtype=np.float64)
            counts = np.zeros((res, res), dtype=np.float64)
            y_idx = np.digitize(points[:, 1], y_edges) - 1
            z_idx = np.digitize(points[:, 2], z_edges) - 1
            valid = (y_idx >= 0) & (y_idx < res) & (z_idx >= 0) & (z_idx < res)
            for i in range(len(points)):
                if valid[i]:
                    grid[z_idx[i], y_idx[i]] += scores[i]
                    counts[z_idx[i], y_idx[i]] += 1.0
            mask = counts > 0
            grid[mask] /= counts[mask]
            # sigma=5.0 fully covers inter-point gaps (~20mm spacing / 5.5mm per pixel ~ 3.6 px)
            grid = gaussian_filter(grid, sigma=5.0)
            # Cosine correction
            cos_factor = np.sqrt(np.clip(1.0 - r2 / R2, 0.0, 1.0))
            grid *= cos_factor
            grid[r2 > R2] = 0.0
            return grid

        grid_cs137 = _project_to_2d_grid(cs137)
        grid_co60 = _project_to_2d_grid(co60)

        # Per-peak pure Gaussian blobs scaled by smoothed grid intensity
        if peaks and len(peaks) > 0:
            sigma_blob = 0.05  # 50mm - smooth blob matching live heatmap appearance

            grid_combined = np.zeros((res, res), dtype=np.float64)

            for i, (py, pz) in enumerate(peaks):
                iso_name = ''
                if peak_labels and i < len(peak_labels):
                    iso_name = peak_labels[i].split(':')[0] if ':' in peak_labels[i] else peak_labels[i]

                # Get amplitude from the smoothed grid at peak pixel position
                # (stable, reflects actual source strength like the live heatmap)
                peak_yi = int(np.clip((py + extent) / (2.0 * extent) * res, 0, res - 1))
                peak_zi = int(np.clip((pz + extent) / (2.0 * extent) * res, 0, res - 1))

                if 'Cs-137' in iso_name:
                    amp = float(grid_cs137[peak_zi, peak_yi])
                elif 'Co-60' in iso_name:
                    amp = float(grid_co60[peak_zi, peak_yi])
                else:
                    amp = float(max(grid_cs137[peak_zi, peak_yi],
                                    grid_co60[peak_zi, peak_yi]))
                if amp < 1e-12:
                    continue

                # Pure Gaussian blob - no grid multiplication
                dist = np.sqrt((YY - py)**2 + (ZZ - pz)**2)
                blob = amp * np.exp(-0.5 * (dist / sigma_blob)**2)

                grid_combined = np.maximum(grid_combined, blob)
        else:
            grid_combined = _project_to_2d_grid(combined)

        # Write rasterised CSV with physical x,y,z coordinates
        raster_path = os.path.join(self.csv_output_dir, "gegi_heatmap_raster.csv")
        x_coord = self.radius
        with open(raster_path, 'w') as f:
            f.write("x,y,z,intensity\n")
            for zi in range(res):
                for yi in range(res):
                    f.write("{:.4f},{:.4f},{:.4f},{:.6f}\n".format(
                        x_coord, yc[yi], zc[zi],
                        grid_combined[zi, yi]))

    def _publish_cloud(self, stamp, norm_scores, iso_norm_scores):
        """Publish colored point-cloud binary blob + JSON metadata header.

        The intensity field contains dose rate in uSv/h.
        RGB is normalized for visual coloring only.
        """
        points = self.sphere_points
        n = points.shape[0]

        # Fields: x, y, z, rgb, intensity, cs137, co60  (7 floats = 28 bytes)
        point_step = 28
        cs137_scores = iso_norm_scores.get('Cs-137', np.zeros(n))
        co60_scores = iso_norm_scores.get('Co-60', np.zeros(n))

        # Normalize scores to [0,1] for RGB coloring only
        vmax = norm_scores.max()
        if vmax > 1e-12:
            # Suppress background noise: zero out below 10% of peak
            threshold = vmax * 0.10
            suppressed = np.where(norm_scores >= threshold, norm_scores, 0.0)
            color_scores = suppressed / vmax
        else:
            color_scores = np.zeros_like(norm_scores)

        buf = bytearray(n * point_step)
        for i in range(n):
            struct.pack_into('fff', buf, i * point_step, points[i, 0], points[i, 1], points[i, 2])
            # Color: blue (cold) -> red (hot) using normalized values
            v = color_scores[i]
            r = int(min(255, v * 2 * 255))
            g = int(min(255, max(0, (v - 0.25) * 2) * 255)) if v > 0.25 else 0
            b = int(max(0, (1.0 - v * 2) * 255)) if v < 0.5 else 0
            rgb_int = (r << 16) | (g << 8) | b
            struct.pack_into('f', buf, i * point_step + 12, struct.unpack('f', struct.pack('I', rgb_int))[0])
            # Intensity field: dose rate, zeroed for background points
            intensity = norm_scores[i] if color_scores[i] > 0 else 0.0
            struct.pack_into('f', buf, i * point_step + 16, intensity)
            struct.pack_into('f', buf, i * point_step + 20, cs137_scores[i] if color_scores[i] > 0 else 0.0)
            struct.pack_into('f', buf, i * point_step + 24, co60_scores[i] if color_scores[i] > 0 else 0.0)

        meta = {
            "stamp": stamp,
            "frame_id": "detector",
            "point_step": point_step,
            "n_points": n,
            "fields": ["x", "y", "z", "rgb", "intensity", "cs137", "co60"],
        }
        self.pub_cloud_meta.send(json.dumps(meta))
        self.pub_cloud.send(bytes(buf))


def main():
    app = prism.Application("spherical_heatmap_node",
                             "GeGi spherical Compton back-projection heatmap", sys.argv)

    app.add_string_option("Heatmap", "isotopes-config", "Path to isotopes.yaml configuration", "")
    app.add_string_option("Heatmap", "nuclide-library",
                          "Path to nuclide_library.yaml, for widening imaging bands to "
                          "nuclides identified by the data recorder's isotope-ID screening "
                          "layer (empty = only the default Cs-137/Co-60 bands)", "")
    app.add_float_option("Heatmap", "radius", "Sphere radius in metres", 0.5)
    app.add_int_option("Heatmap", "n-points", "Number of points on the sphere grid", 8000)
    app.add_float_option("Heatmap", "window-s", "Rolling event window in seconds", 180.0)
    app.add_float_option("Heatmap", "update-period-s", "Heatmap update period in seconds", 2.0)
    app.add_int_option("Heatmap", "min-events", "Minimum events before publishing", 50)
    app.add_int_option("Heatmap", "max-events", "Cap on events fed to back-projection", 20000)
    app.add_float_option("Heatmap", "sigma-floor", "Angular Gaussian sigma (radians)", 0.04)
    app.add_float_option("Heatmap", "max-uncertainty", "Max accepted cone angle uncertainty", 0.15)
    app.add_bool_option("Heatmap", "no-hemisphere-only",
                         "Disable +X-hemisphere-only restriction (default: hemisphere only)")
    app.add_int_option("Heatmap", "max-peaks", "Maximum peaks to report per isotope", 5)
    app.add_float_option("Heatmap", "peak-min-separation-deg",
                         "Minimum angular separation between peaks (degrees)", 25.0)
    app.add_float_option("Heatmap", "peak-threshold",
                         "Secondary peak threshold as fraction of primary", 0.5)
    app.add_bool_option("Heatmap", "no-refine-peaks",
                         "Disable score-weighted centroid peak refinement (default: enabled)")
    app.add_int_option("Heatmap", "peak-persist-frames", "Frames considered for peak persistence", 4)
    app.add_int_option("Heatmap", "peak-persist-min", "Minimum confirming frames for a peak", 2)
    app.add_float_option("Heatmap", "peak-persist-tol-deg",
                         "Angular tolerance for peak persistence matching (degrees)", 8.0)
    app.add_float_option("Heatmap", "support-ratio",
                         "Ghost rejection: min support as fraction of strongest peak", 0.5)
    app.add_int_option("Heatmap", "min-source-events", "Minimum event support for a real source", 15)
    app.add_float_option("Heatmap", "attribution-tol-deg",
                         "Angular tolerance for event-to-peak attribution (degrees)", 6.0)
    app.add_bool_option("Heatmap", "iterative-extraction",
                         "Use CLEAN-style iterative peak extraction instead of support filtering")
    app.add_string_option("Heatmap", "csv-output-dir", "Directory for auto-saved CSV files",
                          "/opt/phds_gegi_driver/data")
    app.add_bool_option("Heatmap", "csv-enabled", "Auto-save CSV files on every update cycle")
    app.add_float_option("Heatmap", "raster-cell-m", "Rasterised CSV cell size in metres", 0.005)
    app.add_float_option("Heatmap", "raster-fov-m", "Rasterised CSV field of view in metres", 0.5)
    app.add_string_option("Heatmap", "node-name",
                          "Used to build gegi.<node-name>.command(_result) topic names",
                          "spherical_heatmap")

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    connection = app.create_connection(result)
    if connection is None:
        logger.error("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    node = SphericalHeatmapNode(app, result, connection)

    while app.is_running():
        time.sleep(0.1)

    node.stop()
    connection.close()


if __name__ == "__main__":
    main()
