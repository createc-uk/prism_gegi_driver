#!/usr/bin/env python3
"""
Prism message schemas for phds_gegi_driver (Python side).

Mirrors include/phds_gegi_driver/messages.hpp and command_channel.hpp
field-for-field. Every processing node hand-rolls JSON via these builders
instead of using prism.JsonSender/JsonReceiver, because the custom message
types used here (ComptonEvent, Spectrum, RunInfo, DetectorInfo,
CommandResult) are not part of Prism's built-in typed-JSON map (only
scalar wrapper types like DoubleValue/IntValue and a handful of geometry
types are). This matches the pattern the C++ driver also uses for the same
custom types, and keeps the wire format identical on both sides.

IMPORTANT: keep field names here in lockstep with
include/phds_gegi_driver/messages.hpp and command_channel.hpp. Nothing
enforces this at compile time -- this file *is* the Python-side contract.
"""

import time


def now_seconds():
    """Seconds since Unix epoch (matches messages::nowSeconds() in C++)."""
    return time.time()


# ---------------------------------------------------------------------------
# Scalar value wrappers (psm::types::DoubleValue / IntValue wire format).
# These match Prism's own built-in typed-JSON shape exactly, so a native
# Prism DoubleValue/IntValue receiver on any language binding can parse them.
# ---------------------------------------------------------------------------

def make_double_value(value):
    return {"dataType": "DoubleValue", "value": float(value)}


def make_int_value(value):
    return {"dataType": "IntValue", "value": int(value)}


def parse_double_value(payload):
    """Returns float value, or None if payload isn't a DoubleValue."""
    if payload.get("dataType") != "DoubleValue":
        return None
    return float(payload["value"])


def parse_int_value(payload):
    """Returns int value, or None if payload isn't an IntValue."""
    if payload.get("dataType") != "IntValue":
        return None
    return int(payload["value"])


# ---------------------------------------------------------------------------
# ComptonEvent -- gegi.driver.compton_event (C++ driver -> heatmap node)
# ---------------------------------------------------------------------------

def make_compton_event(stamp, frame_id, seq,
                        energy_kev_1, reading_location_1,
                        energy_kev_2, reading_location_2,
                        cone_angle, cone_angle_uncertainty):
    """reading_location_* are (x, y, z) tuples/lists, metres."""
    x1, y1, z1 = reading_location_1
    x2, y2, z2 = reading_location_2
    return {
        "dataType": "ComptonEvent",
        "stamp": stamp,
        "frame_id": frame_id,
        "seq": seq,
        "energy_kev_1": energy_kev_1,
        "reading_location_1": {"x": x1, "y": y1, "z": z1},
        "energy_kev_2": energy_kev_2,
        "reading_location_2": {"x": x2, "y": y2, "z": z2},
        "cone_angle": cone_angle,
        "cone_angle_uncertainty": cone_angle_uncertainty,
    }


def parse_compton_event(payload):
    """Returns a plain dict with the same field names (location tuples)."""
    loc1 = payload["reading_location_1"]
    loc2 = payload["reading_location_2"]
    return {
        "stamp": payload["stamp"],
        "frame_id": payload["frame_id"],
        "seq": payload["seq"],
        "energy_kev_1": payload["energy_kev_1"],
        "reading_location_1": (loc1["x"], loc1["y"], loc1["z"]),
        "energy_kev_2": payload["energy_kev_2"],
        "reading_location_2": (loc2["x"], loc2["y"], loc2["z"]),
        "cone_angle": payload["cone_angle"],
        "cone_angle_uncertainty": payload["cone_angle_uncertainty"],
    }


# ---------------------------------------------------------------------------
# Spectrum -- gegi.spectrum.histogram / gegi.spectrum_singles.histogram
# NOTE: `spectrum` carries per-interval DELTA counts, not a running total.
# ---------------------------------------------------------------------------

def make_spectrum(stamp, frame_id, seq, end_time,
                   real_time_ms, dead_time_ms, total_count, spectrum):
    return {
        "dataType": "Spectrum",
        "stamp": stamp,
        "frame_id": frame_id,
        "seq": seq,
        "end_time": end_time,
        "real_time_ms": int(real_time_ms),
        "dead_time_ms": int(dead_time_ms),
        "total_count": int(total_count),
        "spectrum": list(spectrum),
    }


def parse_spectrum(payload):
    return dict(payload)


# ---------------------------------------------------------------------------
# RunInfo -- gegi.detector.run_info (periodic state topic)
# ---------------------------------------------------------------------------

def parse_run_info(payload):
    return dict(payload)


# ---------------------------------------------------------------------------
# DetectorInfo -- gegi.detector.detector_info (periodic state topic)
# ---------------------------------------------------------------------------

def parse_detector_info(payload):
    return dict(payload)


# ---------------------------------------------------------------------------
# CommandResult -- "*.command_result" topics (see prism_command_channel.py)
# ---------------------------------------------------------------------------

def make_command_result(command, success, message, request_id=None, stamp=None):
    out = {
        "dataType": "CommandResult",
        "command": command,
        "success": bool(success),
        "message": message,
        "stamp": now_seconds() if stamp is None else stamp,
    }
    if request_id:
        out["request_id"] = request_id
    return out


def parse_command_result(payload):
    return dict(payload)
