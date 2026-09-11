# DJR Assay — Experiment Runbook

Practical procedures for the six measurements that feed the Design Justification
Report Assay section. Companion to `DJR_assay_measurement_plan.md` (which holds the
report wording). Detector: GeGI planar HPGe, 90 mm crystal (r = 0.045 m), 11 mm
thick. Operational geometry: source 0.33 m from the crystal face, one permanent
5 mm steel plate in the beam; extra plates for hot trays.

## Common setup (all experiments)
- Deploy any config/code change: edit the Windows repo → `~/gegi_ws/sync.sh`
  (+ `~/gegi_ws/build.sh` only for C++) → relaunch `~/gegi_ws/run.sh`.
  `config/isotopes.yaml` is read at **startup**, so calibration/geometry edits
  need a relaunch.
- Set the plate count (operator sets EXTRA plates; the permanent plate is
  automatic): `prism pub --topic gegi.activity.n_shielding_plates --message '{"dataType":"IntValue","value":0}'`
- Check effective geometry: `prism echo --topic gegi.activity.effective_source_distance --max 1`
  (the `DoubleValue.value` should read 0.330 with 0 extra plates).
- Take a timed run: publish `{"command":"start_timed_recording","duration_minutes":N}` to `gegi.data_recorder.command` and monitor `gegi.data_recorder.command_result`.
- Outputs land in `data/` (activity CSV, spectrum N42, heatmaps, manifest).
- Analyse activity vs a certificate: `python tools/validate_efficiency.py --csv <file> --nuclide <Cs137|Co60> --cert-activity <MBq> --cert-date <YYYY-MM-DD> [--tolerance 10]`

> Note on counting windows: the first window of a run can be a partial/ramp-up
> window (few seconds of data labelled with a full 60 s live time) and reads
> spuriously low — discount it when averaging.

---

## A — Background / blank
**Objective:** background in each nuclide ROI → limits of detection (report §2.4),
background uncertainty term (§2.7).
**Procedure:**
1. Empty/representative tray in the operational geometry, no source.
2. Acquire a long spectrum, ≥ 3600 s.
3. Record ROI gross, sideband background `B` and its variance per nuclide.
**Acceptance / output:** a stable background estimate `B` and its uncertainty for
Cs-137 (662 keV) and Co-60 (1173/1332 keV) ROIs, for the intended count time.
**Analyse with:** `python tools/mda.py --csv "<bg>_activity.csv" --config config/isotopes.yaml --count-time 300`
**Status:** ☑ **DONE 2026-07-10** (3600 s blank, `data/Experiment A/`).
MDA @ 300 s: **Cs-137 0.24 kBq, Co-60 4.0 kBq** (both lines) — kBq-scale, 3-4
orders below the MBq tray activities, so detection is never limiting. LoQ (400 ct)
~36 kBq (Cs) / ~78-108 kBq (Co). CAVEAT: Co-60 ROIs showed residual Co-60 (180/80
counts vs 0 for Cs) = sources still in the room, so the Co MDA is a conservative
upper bound; repeat with Co sources cleared for the true figure.

## B — Efficiency validation
**Objective:** confirm the 0.5 m unshielded calibration transfers to the mounted,
shielded geometry; quantify calibration bias (§2.4, §2.5, §2.7).
**Procedure:**
1. `n_shielding_plates = 0` (→ 0.33 m through the permanent plate).
2. One certificated source at a time, on-axis at 0.33 m to the crystal face,
   centred; confirm on-axis with the live heatmap / `/source_direction`.
3. Confirm the expected photopeak(s) are present in the live spectrum BEFORE
   recording (guards against the wrong source / source not in view).
4. Timed run long enough for ≥ 5000 net counts per line (≈10–15 min at ~1 MBq).
5. `validate_efficiency.py` against the certificate; repeat per nuclide.
**Acceptance:** measured activity within ±10 % of the decay-corrected certificate;
Co-60 1173 vs 1332 lines agree (a persistent split indicates an efficiency/µ, not
geometry, problem).
**Status:** ☑ **DONE 2026-07-09.**
- Cs-137 (cert 2.990 MBq): measured −0.5 to −1.1 % → **PASS**, geometry transfer
  confirmed.
- Co-60 (cert 1.130 MBq): read high with a reproducible **4.5 % line split** →
  isolated to the Co calibration factors. Corrected in `isotopes.yaml`
  (Co60_1173 117730→107490, Co60_1332 163393→155990); both now read the
  certificate and agree. **Pending:** fresh Co-60 run after relaunch to confirm
  ~1.115 MBq on both lines.

## C — Rate / dead-time characterisation
**Objective:** maximum measurable activity and the dead-time at that point (§2.3).
**Procedure:**
1. Progressively raise the incident rate: move the source closer, stack sources,
   or remove plates.
2. At each step log input rate, net photopeak throughput (cps) and dead-time %
   from `/detector/get_run_info`.
3. Continue until throughput rolls over (paralysable) or dead-time exceeds ~50 %.
**Acceptance / output:** throughput-and-dead-time vs input-rate curve; pick a
working dead-time cap and convert the corresponding rate to an activity at the
operational geometry → max assayable activity, with and without extra plates.
**Tools:** `tools/deadtime_monitor.py` (instantaneous DT + count_rate_hz from
`get_run_info`, windowed to beat the 1 s realTime granularity).
**Status:** ☑ **DONE 2026-07-09/10 (spec-anchored).** Available sources could not
load the detector past ~2.5% dead-time (field measurements confirmed low DT), and
the ROS event pipeline caps recorded rate ~500 cps while the detector itself
handled ~6 kcps at ~2.5% DT — i.e. the detector is NOT the recorded-rate
bottleneck. The maximum is therefore anchored on the **manufacturer rating: 200
kcps at 10% dead-time in a 15 mR/hr Co-60 field**, converted to activity at the
0.33 m geometry: **~46 MBq Co-60 bare, ~56 MBq through the permanent 5 mm plate**,
DT 10%. Extra plates raise the ceiling ~×1.22 each (~69/84/102 MBq for 1/2/3
added). Two code fixes made so run-info captures during streaming: removed the
`stale_live_time` rejection (killed genuine high-DT frames) and made the frame
scan more robust (attempts 3→6, window 2 s, buffer 64 KB).

## D — Repeatability
**Objective:** Type A (repeatability) uncertainty and count-time precision
(§2.5, §2.7); firm up the absolute Co-60 scale after the B correction.
**Procedure:**
1. Fixed reference source and geometry.
2. ≥ 10 repeat timed runs at the nominal count time (reposition/replace the
   source between runs to capture set-up variation, if that reflects operations).
3. Compute mean and standard deviation of the reported activity.
**Acceptance / output:** repeatability SD (%) per nuclide; confirms the count time
delivers the target precision.
**Analyse with:** `python tools/repeatability.py --dir "<folder>" --nuclide Co60 --cert-activity 1.130 --cert-date 2026-06-01` (auto-excludes partial start-up windows).
**Status:** ☑ **DONE 2026-07-09** (10 x 5-min Co-60, `data/Experiment D/`).
- Repeatability **RSD = 2.1%** (Type A, k=1) on combined activity.
- Accuracy: mean 1.128 MBq vs cert 1.115 MBq → **+1.2%**; lines 1173 −0.1%,
  1332 +2.6% (agree to 2.7%, was 4.5% pre-B). Confirms the B calibration fix.
- Residual: 1332 +2.6% — optional refine CF 155990→152010 to centre on cert
  using this 10-run dataset.

## E — Position / heterogeneity sensitivity
**Objective:** bound the within-tray source-position term — usually the dominant
real-world systematic (§2.7).
**Procedure:**
1. One source; assay at tray centre, each corner, and two heights.
2. Record the activity readout at each position.
3. Use the Compton image to show how position is detected/corrected.
**Acceptance / output:** spread of activity vs position (%) → the position/
heterogeneity uncertainty contribution.
**Analyse with:** `python tools/position_check.py --nuclide Co60 --tray 0.35 --standoff 0.33 --runs center:c.csv corner:a.csv ...`
**Status:** ☑ **DONE 2026-07-10** (Co-60 at centre + 4 corners of a 35 cm tray,
`data/Experiment E/`). Corner reads **31% LOW** vs centre (corner ~0.39 m/37° off
-axis vs assumed 0.33 m on-axis); **SD across positions 18.8%** = the DOMINANT
uncertainty term. Fully explained by inverse-square (predicted 69-71% vs measured
69%). CORRECTABLE via the Compton image (activity node currently uses fixed
on-axis 0.33 m and ignores the imaged position — biggest accuracy improvement
available).

## F — Shielding transmission check
**Objective:** confirm the steel µ values used for the attenuation correction
(§2.3, §2.4, §2.7).
**Procedure:**
1. Fixed source and geometry.
2. Assay with 0, 1, 2, 3 EXTRA plates (total 1–4 including the permanent plate);
   set via `/activity/n_shielding_plates`.
3. Compare the measured net-rate ratio between plate counts to the model
   `exp(-µ·n·t)` (t = 0.005 m per plate).
**Acceptance / output:** measured vs modelled transmission per nuclide; adjust
`mu_shield_per_m` if they disagree beyond the target uncertainty.
**Analyse with:** `python tools/shielding_check.py --config config/isotopes.yaml --runs 0:p0.csv 1:p1.csv 2:p2.csv 3:p3.csv` (number = EXTRA plates; add `--moving-source` if plates pushed the source away). Reports measured vs model transmission + fitted mu per line.
**Status:** ☑ **DONE 2026-07-10** (Co-60, 1/2/3 total plates, `data/Experiment F/`, moving-source).
Fitted mu 44.1 /m (1173) & 41.5 /m (1332) vs config 41.8/39.6 → **confirmed within
~5%** (measured attenuation slightly stronger than standard-iron; ~1% activity
impact at 1 plate). Cs-137 (662 keV) mu NOT tested (no Cs source) — retains 57.4 /m.
Config mu KEPT (within uncertainty).

---

## Progress summary
| Exp | Feeds report item | Status |
|-----|-------------------|--------|
| A — Background / blank | Limits of detection | ☑ done (MDA ~0.24/4 kBq @300s) |
| B — Efficiency validation | Calibration / count times | ☑ done (Co-60 CFs corrected) |
| C — Rate / dead-time | Max activity + dead-time | ☑ done (spec: ~56 MBq @ 10% DT) |
| D — Repeatability | Count times / uncertainty | ☑ done (RSD 2.1%, +1.2% accuracy) |
| E — Position sensitivity | Uncertainty | ☑ done (31% corner, 18.8% SD, dominant) |
| F — Shielding transmission | Max activity / MDA / uncertainty | ☑ done (mu confirmed ~5%) |

Reference sources used in B: Cs-137 2.990 MBq, Co-60 1.130 MBq, both certificated
to 2026-06-01. Result files under `data/Experiment B/`.
