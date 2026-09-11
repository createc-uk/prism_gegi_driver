# Design Justification Report — Assay Section: Measurement Plan & Report Wording

**Purpose.** Provide the evidence Chris Goddard requires for the DJR Assay section:
optioneering reference, waste summary, maximum measurable activity + dead-time,
limits of detection, expected count times, volume throughput, and an uncertainty
assessment. This document proposes the measurements to perform on the GeGI and
gives report wording (with `[placeholders]` to fill from the results).

Detector: GeGI planar HPGe, 90 mm diameter crystal (r = 0.045 m), 11 mm thick,
~61 cm² active area. Operational geometry: source ~0.33 m from the crystal face,
one permanent 5 mm steel plate in the beam, additional 5 mm plates for hot trays.
Nuclides of interest: Cs-137 (662 keV) and Co-60 (1173 / 1332 keV).

---

## 1. Measurement campaign (do these six; they feed every report item)

| # | Experiment | Method | Feeds report item |
|---|-----------|--------|-------------------|
| **A** | **Background / blank** | Acquire a long (≥3600 s) spectrum with an empty/representative tray in the operational geometry. Record ROI gross, sideband background B and its variance. | LoD (4), uncertainty background term (7) |
| **B** | **Efficiency validation** | Known-activity (certificated) Cs-137 and Co-60 point sources at 0.33 m with 1 plate. Compare reported activity to certificate (decay-corrected). This is the validation shot for the unshielded-0.5 m calibration transferred to the new geometry. | Confirms efficiency for (4)(5)(7); calibration uncertainty (7) |
| **C** | **Rate / dead-time characterisation** | Progressively raise the incident rate (move source closer, stack sources, or remove plates). At each step log input rate, throughput (net photopeak cps) and reported dead-time % from the periodic `gegi.detector.run_info` state topic. Continue until throughput rolls over or dead-time exceeds ~50 %. | Max activity + max dead-time (3) |
| **D** | **Repeatability** | ≥10 repeat measurements of one reference source, fixed geometry, at the nominal count time. Compute mean and standard deviation. | Count-time precision (5), Type A uncertainty (7) |
| **E** | **Position / heterogeneity sensitivity** | Move a source to tray centre, corners and two heights; assay each. Records how activity readout varies with source location within the tray (the dominant real-world systematic; the Compton image is used to bound it). | Uncertainty geometry/heterogeneity term (7) |
| **F** | **Shielding transmission check** | Assay one source with 0/1/2/3 added plates (total 1–4). Compare measured transmission to `exp(-µ·n·t)`. | Confirms µ for (3)(4); shielding uncertainty (7) |

All six use only sources and hardware you already have. A–B–F can be one session
with a single Co-60 and a single Cs-137 source; C needs a hotter source or closer
standoff; D–E are repeats/repositions of B.

---

## 2. Item-by-item: what to measure, how to compute, how to word it

### 2.1 Reference back to the optioneering
*No new measurement — narrative cross-reference.*

> **Report wording.** "As established in the optioneering study [ref], the GeGI
> HPGe imaging spectrometer was selected because it uniquely satisfies both the
> **system** need (compact, in-line assay compatible with the Auto-SAS sorting
> cadence) and the **characterisation** need (high-resolution nuclide
> identification, quantitative activity assay, and Compton-imaging spatial
> localisation of activity within a tray). The high energy resolution (FWHM
> ~[x] keV at 1332 keV) resolves the Cs-137 and Co-60 lines of interest without
> interference, while the imaging capability discriminates tray-borne activity
> from background and neighbouring trays. This section justifies that the
> selected instrument meets the quantified assay requirements derived below."

### 2.2 Summary of the waste information
*No GeGI measurement — draw from the waste stream characterisation data.* State
the envelope the assay must cover, because it sets the targets for items 3–6.

> **Report wording.** "The waste stream comprises [form/matrix] in [tray type],
> nominal volume [V] m³ and matrix density [ρ] g/cm³. The radionuclides of
> concern are Cs-137 and Co-60, with expected per-tray activities from
> [A_min] MBq (lower activity of interest) to [A_max] MBq (bounding hot tray),
> and associated surface dose rates of [d_min]–[d_max] µSv/h. Activity is assumed
> [homogeneously distributed / potentially localised], which the assay design
> accommodates as described in §2.8 (uncertainty)."

### 2.3 Maximum activity that can be measured, with estimated maximum dead-time
**Measure (Experiment C).** Raise incident rate in steps; at each step record
input count rate (ICR), net photopeak throughput (OCR) and dead-time % from
`get_run_info`. Plot OCR and dead-time vs ICR. The detector is paralysable:
`OCR ≈ ICR·exp(-ICR·τ)`, peaking at `ICR = 1/τ`. Choose a **working dead-time
limit** (recommend **≤ 40 %**) below which: (i) the live-time correction is
reliable, and (ii) pile-up / random-summing distortion of the photopeak is small.
Convert the ICR at that limit back to activity at the operational geometry using
the calibrated response (Experiment B). Report both with and without extra plates,
since each 5 mm plate lowers the incident rate and so raises the assayable
activity ceiling.

> **Report wording.** "The maximum activity assayable in the operational geometry
> (0.33 m, one 5 mm plate) is **[A_max_meas] MBq** of [nuclide], corresponding to
> a detector dead-time of **[DT_max] %**, chosen as the point below which
> live-time correction and pile-up remain within tolerance. Above this the
> throughput becomes paralysable and quantitative correction is unreliable. For
> hotter trays, inserting additional 5 mm steel plates reduces the incident rate
> (each plate transmits ~75 % at 662 keV / ~82 % at 1332 keV), extending the
> ceiling to **[A_max_3plate] MBq** with 3 added plates while holding dead-time
> below [DT_max] %. The measured throughput/dead-time curve is given in
> Figure [n]."

*Note for the report:* for Co-60 at 0.33 m, true-coincidence summing of the
1173/1332 cascade is a small but non-zero effect; state whether a summing
correction is applied or folded into uncertainty (§2.8).

### 2.4 Limits of detection
**Measure (Experiment A).** From the blank spectrum, take background counts `B`
in each nuclide ROI over the intended count time `T`. Compute the Currie (1968)
detection limit (counts, 95 % confidence, paired blank):

```
Critical level      L_C = 2.33·√B
Detection limit     L_D = 2.71 + 4.65·√B      [counts]
```

Convert to Minimum Detectable Activity using the operational-geometry response:

```
MDA (Bq) = L_D / ( ε_int · Ω(d)/4π · I_γ · t_live · T_shield )
```

where `ε_int` is the intrinsic full-energy-peak efficiency (detector constant,
derived from the calibration factor), `Ω(d)/4π` the solid-angle fraction at
0.33 m, `I_γ` the emission probability, `t_live` the live time, and `T_shield`
the plate transmission. Report MDA per nuclide, for the nominal count time, both
with 1 plate and with the maximum plate count (shielding raises MDA by 1/T_shield,
i.e. ~×1.33 per plate at 662 keV).

Distinguish **detection** from **quantification**: the driver's validity gate of
≥400 net counts corresponds to a ~5 % counting precision — an implicit *limit of
quantification* (LoQ ≈ 10σ) that is stricter than the MDA.

> **Report wording.** "The Minimum Detectable Activity (Currie, 95 % confidence)
> for a [T]-second assay in the operational geometry is **[MDA_Cs] MBq**
> (Cs-137) and **[MDA_Co] MBq** (Co-60). These lie a factor of [k] below the
> lower activity of interest ([A_min] MBq), confirming the GeGI reliably detects
> the nuclides of concern across the required range. The corresponding limit of
> quantification (≥400 net counts, ≤5 % counting uncertainty) is [LoQ] MBq. With
> the maximum shielding configuration ([n] plates) the MDA rises to [MDA_shielded]
> MBq, still below [A_min]."

### 2.5 Expected count times
**Derive + confirm (from B and D).** The count time is set by the requirement to
reach the quantification threshold (≥400 net counts, ≤5 % counting uncertainty)
at the bounding case — lowest activity of interest, maximum shielding:

```
T_live = N_req / ( A · ε_int · Ω(d)/4π · I_γ · T_shield )      with N_req = 400
```

Compute the net photopeak cps per becquerel from Experiment B, then tabulate the
count time versus tray activity. Confirm empirically with a representative source.

> **Report wording.** "Assay count time is governed by accumulating ≥400 net
> photopeak counts (≤5 % counting uncertainty). At the nominal tray activity of
> [A_nom] MBq (0.33 m, one plate) this is reached in **[t_nom] s**; the bounding
> case ([A_min] MBq at maximum shielding) requires **[t_max] s**. A nominal assay
> time of **[T] s** is therefore specified, providing margin across the expected
> activity range (Table [n]). Measured count times agreed with prediction to
> within [x] %."

*(Illustrative, to be confirmed by Experiment B: with the current Cs-137
calibration the net photopeak rate at 0.33 m through one plate is of order
~10⁻⁵ net cps per Bq, so a ~1 MBq tray reaches 400 net counts in tens of seconds.
The measured value from B replaces this estimate in the report.)*

### 2.6 Expected volume throughput
**Derive.** Combine the specified assay time with the mechanical tray index /
handling time of the Auto-SAS cycle:

```
Trays per hour = 3600 / ( T_assay + T_handle )
Volume per hour = trays_per_hour · V_tray
```

Include per-tray software overhead (spectrum readout, imaging update, N42/CSV
save) in `T_handle`.

> **Report wording.** "With the specified **[T] s** assay time plus **[T_handle] s**
> tray indexing and data handling, the system achieves **[N] trays/hour**,
> equivalent to **[Vdot] m³/hour** at the nominal [V_tray] m³ tray volume. This
> [meets / exceeds] the system throughput requirement of [req] m³/hour. Throughput
> for hot trays requiring longer counts or additional shielding is [N_hot]
> trays/hour."

### 2.7 Uncertainty assessment
**Measure the empirical terms (D, E, F).** Build a GUM-style budget combining
Type A (repeatability, from Experiment D) and Type B contributions. Combine in
quadrature; report expanded uncertainty at k = 2 (95 %).

| Component | Type | Source of estimate | Typical magnitude |
|-----------|------|--------------------|-------------------|
| Counting statistics | A | √N/N at the assay count | dominant at low A |
| Repeatability | A | SD of 10 repeats (Exp D) | `[s]` % |
| Calibration source certificate | B | source certificate | `[c]` % (typ. 2–3 %) |
| Efficiency transfer to geometry | B | Exp B residual | `[e]` % |
| Standoff / solid angle | B | ±[Δd] cm → dΩ/Ω | `[g]` % |
| Source position / heterogeneity in tray | B | Exp E spread | `[p]` % (often dominant) |
| Shielding transmission (µ, plate count) | B | Exp F residual | `[sh]` % |
| Dead-time / live-time correction | B | from Exp C at op. rate | `[dt]` % |
| Background subtraction / interference | B | Exp A | `[bg]` % |
| True-coincidence summing (Co-60) | B | calc at 0.33 m | `[sum]` % |

```
u_combined = √( Σ u_i² )        U(95%) = 2 · u_combined
```

> **Report wording.** "The activity uncertainty budget (Table [n]) combines Type A
> repeatability ([s] %, from [n] repeat assays of a reference source) with Type B
> contributions from the calibration certificate ([c] %), efficiency transfer
> ([e] %), standoff ([g] %), within-tray source position/heterogeneity ([p] %),
> shielding transmission ([sh] %), dead-time correction ([dt] %), background
> ([bg] %) and Co-60 coincidence summing ([sum] %). Combined in quadrature, the
> expanded uncertainty (k = 2, 95 %) on a single-tray activity assay is
> **±[U] %**. The dominant term is [component]. This meets the [req] % assay
> uncertainty requirement. Where activity is localised rather than homogeneous,
> the Compton image is used to [correct the assumed geometry / flag the tray for
> re-assay], bounding the position term to the value quoted above."

---

## 3. Assumptions & notes
- Efficiency is treated as a geometry-independent detector constant (intrinsic
  FEP efficiency) derived from the calibration factor; distance and shielding are
  applied analytically. Experiment B validates this transfer — no full
  recalibration is required unless B disagrees by more than the target uncertainty.
- Do **not** recalibrate *through* the shield while the transmission correction is
  active — that double-counts the steel.
- All µ values (57.4 / 41.8 / 39.6 m⁻¹ for 662 / 1173 / 1332 keV) are
  standard-iron approximations; Experiment F confirms them for the actual steel.
- Dead-time is read live during acquisition via `/detector/get_run_info`; the
  reported live time drives all activity/MDA calculations.
