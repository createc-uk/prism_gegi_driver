# -*- coding: utf-8 -*-
"""The CURRENTLY COMMISSIONED rig configuration.

>>> IF THE RIG CHANGES, EDIT THIS FILE — AND ONLY THIS FILE. <<<

A new standoff, a different shielding material, or a different plate thickness
changes these numbers. The *physics* tests are parameter-driven (they pass mu,
thickness and distance in as arguments) and are unaffected by any of it — only
the configuration-snapshot assertions in test_config_contract.py read this file.

Keeping the snapshot is deliberate: it means an accidental edit to
config/isotopes.yaml (a zeroed calibration factor, a mistyped standoff) fails a
test instead of silently corrupting every reported activity.
"""

# --- Geometry (2026-08-06 rig: 580 mm, plywood table + permanent steel plate) --
# The permanent 11 mm plywood + 5 mm steel plate are absorbed into the calibration
# (calibrate-in-place), so BASE_SHIELD_PLATES = 0: only EXTRA plates are corrected.
BARE_STANDOFF_M = 0.580       # measured face-to-source with 0 EXTRA plates
PLATE_THICKNESS_M = 0.005     # thickness of ONE extra plate (5 mm)
BASE_SHIELD_PLATES = 0        # permanent plate is in the CF, not corrected
OPERATIONAL_STANDOFF_M = 0.580  # = BARE + BASE_SHIELD_PLATES * PLATE_THICKNESS

CRYSTAL_RADIUS_M = 0.045      # GeGi 90 mm diameter crystal
CALIBRATION_DISTANCE_M = 0.580  # calibration = operational geometry (in-place)

# --- Shielding ------------------------------------------------------------
SHIELD_MATERIAL = 'steel'
# Linear attenuation coefficient (1/m) per gamma line, for SHIELD_MATERIAL.
# Change the whole dict if the material changes.
# Shielding model (2026-09-04, clean source-untouched sequences): the
# MEASURED per-plate-count broad-beam transmissions below rule for 1-2
# plates (build-up grows with thickness, so a single exponential misfits);
# MU_PER_M is only the >2-plate extrapolation slope (Cs: clean 0/1/2 fit;
# Co: 2-plate LSQ compromise).
MU_PER_M = {
    'Cs137': 48.6,        # 662 keV (effective, measured clean)
    'Co60_1173': 35.5,    # 1173 keV (pooled extrapolation slope; table rules 1-2)
    'Co60_1332': 35.0,    # 1332 keV (pooled extrapolation slope; table rules 1-2)
}
PLATE_TRANSMISSION = {
    'Cs137': {1: 0.7915, 2: 0.6125},
    'Co60_1173': {1: 0.835, 2: 0.7178},   # T(1) = mean of two untouched pairs
    'Co60_1332': {1: 0.825, 2: 0.7028},
}

# --- Nuclides expected in the assay --------------------------------------
EXPECTED_LINES = ('Cs137', 'Co60_1173', 'Co60_1332')

# --- Dose-rate gamma constants (uSv*m^2 / MBq*h) --------------------------
# Physical constants, not rig-dependent — they only change if a nuclide is added.
GAMMA_CONSTANTS = {'Cs137': 0.0771, 'Co60': 0.3059}
