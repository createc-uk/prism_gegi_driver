# Porting / cloning this driver to another machine

What a fresh clone needs to build, run, and — most importantly — produce
*correct* activity numbers. Read §1 first: the driver can run on another detector
while silently reporting incorrect activity if calibration and geometry are not
recommissioned.

## 1. The calibration is detector-specific

`config/isotopes.yaml` contains `calibration_factor` values derived for one GeGi
unit and corrected against certificated sources. The shielding geometry also
describes one physical rig.

If the detector or rig changes:

1. Re-run Experiment B in `docs/DJR_assay_experiments.md` with a certificated
   source and compare against its decay-corrected certificate.
2. Update `config/isotopes.yaml`: line calibration factors, shielding geometry,
   material, and `mu_shield_per_m` where applicable.
3. Update `test/commissioned.py`, the only test module that deliberately encodes
   rig-specific values.
4. Run `bash test/run_tests.sh`. A commissioned-configuration failure means the
   YAML and declared rig no longer agree.

Example analysis command:

```bash
python3 tools/validate_efficiency.py --csv <run>_activity.csv \
  --nuclide Co60 --cert-activity 1.130 --cert-date 2026-06-01
```

## 2. Environment

| Component | Requirement |
|---|---|
| C++ | C++20 compiler and CMake 3.20+ |
| Messaging | Vendored Prism plus a built transport; NATS is enabled by default |
| Native libraries | Boost system/thread/regex and libsodium |
| Python | Python 3.8+, Prism bindings, NumPy, PyYAML, SciPy, Matplotlib |
| Python binding build | Python development headers and OpenCV development libraries |
| Supported container build | Ubuntu 22.04 (Boost 1.74) |

The detector driver is Linux-oriented. Plotting tools can run natively on Windows
and connect directly to Prism/NATS; no ROS or websocket bridge is required.

## 3. Clone and build

Prism has nested submodules:

```bash
git clone <repo-url> phds_gegi_driver
cd phds_gegi_driver
git submodule update --init --recursive
```

Build the C++ driver and Prism CLI:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

Install the Python bindings separately:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install ./deps/prism/bindings/python
python3 -m pip install numpy PyYAML scipy matplotlib
```

The standard CMake outputs are `build/bin/phds_gegi_driver_node` and
`build/bin/prism`.

> The detector transport uses legacy Boost.Asio APIs. The maintained Docker
> image pins Ubuntu 22.04/Boost 1.74. A host with Boost 1.90 cannot compile the
> unchanged `tcp_event_reader` until that transport is separately modernised.

## 4. Site-specific settings

| Setting | How to override | Note |
|---|---|---|
| Detector IP | `DETECTOR_IP` / `GEGI_IP` | Default `192.168.50.109` |
| Detector port | `DETECTOR_PORT` / `GEGI_PORT` | Default `27015` |
| Messaging protocol | `PRISM_PROTOCOL` | Default `nats` |
| Messaging server | `PRISM_SERVER`, `PRISM_PORT` | Default `localhost:4222` |
| Recorder output | `OUTPUT_DIR` | Default `<repo>/data` |
| Calibration | `CALIBRATION_FILE`, `ISOTOPES_CONFIG` | Must match the commissioned unit/rig |
| Driver topics | `config/gegi_driver.yaml` | Prism endpoint-name overrides |

Start NATS, then launch the complete stack:

```bash
nats-server
./scripts/run_full_pipeline.sh
```

Example overrides:

```bash
DETECTOR_IP=192.168.50.110 OUTPUT_DIR="$HOME/gegi_data" \
PRISM_SERVER=localhost PRISM_PORT=4222 ./scripts/run_full_pipeline.sh
```

The detector permits only the connections managed by the C++ driver. External
applications must consume Prism topics and must not open their own detector link.

## 5. Verify the clone

Run tests before connecting hardware:

```bash
bash test/run_tests.sh
```

The suite needs no detector or NATS server, but the Prism Python bindings must be
importable. Then start NATS and inspect live state/data with the Prism CLI:

```bash
./build/bin/prism hz --protocol nats --ip localhost --port 4222 \
  --topic gegi.driver.compton_event --window 5
./build/bin/prism echo --protocol nats --ip localhost --port 4222 \
  --topic gegi.detector.detector_info --max 1
```

See `README.md` for command publication examples and
`docs/PRISM_MIGRATION.md` for the complete interface and schemas.

## 6. Known gotchas

- Keep shell scripts LF-terminated; `.gitattributes` enforces this.
- `gegi.spectrum.histogram` contains per-interval **deltas**. The recorder
  integrates them; changing this to cumulative counts corrupts saved N42 files.
- Energies above calibration currently enter the top spectrum bin. This is an
  existing, tested behavior.
- Core pub/sub is not retained state. The activity distance and detector state
  feeds are periodically republished so late subscribers converge.
- `gegi.heatmap.cloud_meta` and binary `gegi.heatmap.cloud` are separate
  messages; consumers cache metadata before decoding the 28-byte point layout.
- Recorder event output is JSONL, not a ROS bag. Replay software must choose its
  own pacing from event timestamps or a configured rate.
