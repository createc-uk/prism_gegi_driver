# PHDS GeGi Driver for Prism

This repository connects a PHDS GeGi detector to a Prism/NATS processing pipeline for spectra, Compton imaging, isotope activity estimation, recording, and live plotting.

The detector-side boundary has not changed: the C++ driver still speaks the GeGi vendor TCP protocol directly to the detector. Only the network-facing application transport changed—from ROS/catkin/rosbridge interfaces to Prism publishers, subscribers, and command channels carried by NATS.

For interface mappings and migration detail, see [docs/PRISM_MIGRATION.md](docs/PRISM_MIGRATION.md). This README covers build and day-to-day use only.

## Prerequisites

- A reachable, powered, and cooled GeGi detector (default `192.168.50.109:27015`).
- Git with submodule support.
- CMake 3.20+ and a C++20 compiler (GCC 10+, Clang 12+, or MSVC 2019+).
- Boost system, thread, and regex development libraries.
- Python 3.8+ with development headers and `pip`.
- OpenCV development libraries, required when building the vendored Prism Python bindings.
- Python runtime packages used by the processing/plotting tools: NumPy, PyYAML, SciPy, and Matplotlib.
- A NATS 2.x server, installed locally or run with Docker.

On Ubuntu/Debian, the main system packages can be installed with:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git libboost-system-dev libboost-thread-dev libboost-regex-dev python3-dev python3-pip libopencv-dev
```

## Clone and initialise submodules

Prism and its dependencies are vendored as nested submodules, so initialise recursively:

```bash
git submodule update --init --recursive
```

## Build the C++ driver and Prism CLI

The top-level CMake project builds the GeGi driver against the vendored Prism NATS backend. It deliberately does not build the Python bindings in this build tree.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

The relevant executables are:

- `build/phds_gegi_driver_node`
- `build/bin/prism`

Optionally install the Prism CLI onto `PATH` with `sudo cmake --install build`; otherwise use `./build/bin/prism` in the examples below.

## Install the Prism Python bindings separately

Use a virtual environment if desired, then follow the vendored binding's `scikit-build-core`/`pip` installation path:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install ./deps/prism/bindings/python
python3 -m pip install numpy PyYAML scipy matplotlib
python3 -c "import prism; print(prism.__file__)"
```

This separate install is required because the driver CMake project forces `PSM_BUILD_PYTHON_BINDINGS=OFF`. The Python nodes, live plotting tools, and node-importing tests all require `import prism` to succeed in the selected Python environment.

## Run NATS

Run a local server directly:

```bash
nats-server
```

Or run one with Docker:

```bash
docker run --rm -d --name gegi-nats -p 4222:4222 nats:latest
```

The default Prism connection is NATS at `localhost:4222`.

## Run the pipeline

Start the complete stack with the Prism-native launcher:

```bash
./scripts/run_full_pipeline.sh
```

It starts the C++ driver, main and singles spectrum instances, activity estimator, spherical heatmap, and data recorder, and forwards `SIGINT`/`SIGTERM` to every child process. Connection and detector settings can be overridden with environment variables:

```bash
PRISM_PROTOCOL=nats PRISM_SERVER=localhost PRISM_PORT=4222 \
DETECTOR_IP=192.168.50.109 DETECTOR_PORT=27015 \
OUTPUT_DIR="$PWD/data" ./scripts/run_full_pipeline.sh
```

The old ROS launch XML files have been removed. To run components individually, use the following commands in separate terminals from the repository root (and activate the Python environment in each Python terminal):

```bash
# Detector TCP driver
./build/phds_gegi_driver_node \
  --protocol nats --server localhost --port 4222 \
  --gegi-ip 192.168.50.109 --gegi-port 27015
```

```bash
# Spectrum accumulator
python3 src/phds_gegi_driver/spectrum_node.py \
  --protocol nats --server localhost --port 4222 \
  --calibration-file config/EnergyCal_15000_0p2keV.csv
```

```bash
# Spherical Compton heatmap
python3 src/phds_gegi_driver/spherical_heatmap_node.py \
  --protocol nats --server localhost --port 4222 \
  --isotopes-config config/isotopes.yaml
```

```bash
# Per-isotope activity estimator
python3 src/phds_gegi_driver/activity_node.py \
  --protocol nats --server localhost --port 4222 \
  --calibration-file config/EnergyCal_15000_0p2keV.csv \
  --isotopes-config config/isotopes.yaml
```

```bash
# Timed-acquisition recorder
python3 src/phds_gegi_driver/data_recorder_node.py \
  --protocol nats --server localhost --port 4222 \
  --calibration-file config/EnergyCal_15000_0p2keV.csv \
  --isotopes-config config/isotopes.yaml \
  --output-dir data
```

The launcher also starts the singles spectrum instance on `gegi.spectrum_singles.histogram`. Use each application's `--help` output for additional processing and connection options.

## Docker deployment (recommended for portable operation)

This repository includes a **self-contained Docker build** of the GeGi C++
driver, Prism CLI, Python bindings, calibration files and all processing
nodes. You do **not** need ROS, CMake or Python installed on the deployment PC.
The only runtime prerequisite is Docker Engine with the Compose v2 plugin.
The current build has been verified on Linux/amd64; other architectures need
a separately built image and verification.

Clone with the Prism submodule on a machine that has access to Createc's
private repositories, then build and run:

```bash
git clone --recursive git@github.com:createc-uk/prism_gegi_driver.git
cd prism_gegi_driver
mkdir -p data
DETECTOR_IP=192.168.50.109 DETECTOR_PORT=27015 docker compose up -d --build
```

The Compose stack starts NATS and the six-node GeGi pipeline. Set
`DETECTOR_IP` and `DETECTOR_PORT` to the detector's reachable network
address. **Do not run this against hardware before the site test is approved.**
The detector must be powered, cooled and reachable from the container.

View process logs and stop the system:

```bash
docker compose logs -f app
docker compose down
```

Measurement outputs are ordinary host files in `./data` by default. Override
with `GEGI_DATA_DIR=/some/host/directory`. Linux bind-mounted files may be
owned by root because the container currently runs as root.

Commands are plain Prism messages; with Compose you do not need to install
the CLI on the host. For example:

```bash
# Live spectrum updates (Ctrl+C quits the observer):
docker compose exec app prism echo --protocol nats --ip nats --port 4222 \
  --topic gegi.spectrum.histogram

# Start a five-minute recording; this clears all detector windows and buffers:
docker compose exec app prism pub --protocol nats --ip nats --port 4222 \
  --topic gegi.data_recorder.command \
  --message '{"command":"start_timed_recording","duration_minutes":5}'

# Stop early and save the recording:
docker compose exec app prism pub --protocol nats --ip nats --port 4222 \
  --topic gegi.data_recorder.command \
  --message '{"command":"stop_recording"}'
```

Watch responses on `gegi.data_recorder.command_result` in another terminal.
A successful command result means the byte was sent, **not** that hardware
confirmed its physical state. Recordings also generate N42, activity CSV,
JSONL, heatmap outputs and a manifest in `./data`.

### Move the image to another computer

Build once on the same CPU architecture as your deployment machine, then
transfer the image without needing compilers or a live connection to the
private Prism submodule:

```bash
docker image save prism-gegi-driver:latest | gzip > prism-gegi-driver.tar.gz
# On the destination:
docker image load -i prism-gegi-driver.tar.gz
```

Copy `docker-compose.yml` and create the destination `data/` directory.
That Compose file still contains a `build` stanza for source installs;
`docker compose up -d --no-build` uses the transferred local image.
NATS is a separate image (`nats:2-alpine`) and must be pulled or exported
and transferred as well for fully offline deployment.

By default, the exposed NATS port binds only to `127.0.0.1:4222` on the
host. Change `NATS_HOST_PORT` if another service uses that port. Remote
connections require `NATS_BIND_ADDR` to be set explicitly and appropriate
network isolation/authentication; do **not** expose unauthenticated NATS on a
site network.

For repeatable **hardware-free** verification (Docker and host Python 3
only), build the image and run:

```bash
python3 test/docker_compose_smoke.py
```

This uses `test/docker-compose.mock.yml` as an explicit Compose override,
runs a fake detector on the **private Compose network**, and tests actual
NATS service discovery, six-node startup, acquisition commands and durable
host-mounted N42/CSV/JSONL outputs. The simulator port is not published.
It automatically removes the temporary containers/network and test data.
No physical detector is contacted.

Alternatively, `python3 test/simulated_pipeline_smoke.py --docker` validates
the same image using isolated loopback sockets with Linux host networking.
The native counterpart is `python3 test/simulated_pipeline_smoke.py`.

## Prism topics

All application topics use NATS-compatible dot-separated names. The principal interfaces are:

| Area | Topics |
|---|---|
| Raw driver data | `gegi.driver.compton_event`, `gegi.driver.energy_deposit`, `gegi.driver.energy_deposit_singles` |
| Detector state | `gegi.detector.run_info`, `gegi.detector.detector_info`, `gegi.detector.dead_time_percent` |
| Detector commands | `gegi.detector.command`, `gegi.detector.command_result` |
| Spectrum | `gegi.spectrum.histogram`, `gegi.spectrum.command`, `gegi.spectrum.command_result` |
| Heatmap | `gegi.heatmap.cloud` (binary), `gegi.heatmap.cloud_meta`, `gegi.heatmap.source_direction`, `gegi.heatmap.source_directions`, `gegi.heatmap.source_isotopes` |
| Activity | `gegi.activity.total_activity`, `gegi.activity.results`, `gegi.activity.effective_source_distance`, `gegi.activity.source_distance`, `gegi.activity.n_shielding_plates` |
| Activity commands | `gegi.activity.command`, `gegi.activity.command_result` |
| Recorder commands | `gegi.data_recorder.command`, `gegi.data_recorder.command_result` |

`gegi.detector.run_info` and `gegi.detector.detector_info` are continuously published state topics; they replace the former on-demand state services.

## Inspect and publish with the Prism CLI

The Prism CLI uses `--ip`, while Prism applications use `--server`. These examples match the vendored CLI implementation.

```bash
# Print five spectra
./build/bin/prism echo --protocol nats --ip localhost --port 4222 \
  --topic gegi.spectrum.histogram --max 5

# Monitor the event rate (five-second statistics window)
./build/bin/prism hz --protocol nats --ip localhost --port 4222 \
  --topic gegi.driver.compton_event --window 5

# Read one state update
./build/bin/prism echo --protocol nats --ip localhost --port 4222 \
  --topic gegi.detector.run_info --max 1
./build/bin/prism echo --protocol nats --ip localhost --port 4222 \
  --topic gegi.detector.detector_info --max 1
```

Commands are JSON messages. Subscribe to the result topic before publishing when a response is needed:

```bash
./build/bin/prism echo --protocol nats --ip localhost --port 4222 \
  --topic gegi.detector.command_result
```

```bash
./build/bin/prism pub --protocol nats --ip localhost --port 4222 \
  --topic gegi.detector.command \
  --message '{"command":"start_acquisition","request_id":"cli-start-1"}'

./build/bin/prism pub --protocol nats --ip localhost --port 4222 \
  --topic gegi.detector.command \
  --message '{"command":"stop_acquisition","request_id":"cli-stop-1"}'
```

To start a five-minute acquisition and record all pipeline outputs, subscribe to `gegi.data_recorder.command_result` and publish:

```bash
./build/bin/prism pub --protocol nats --ip localhost --port 4222 \
  --topic gegi.data_recorder.command \
  --message '{"command":"start_timed_recording","duration_minutes":5,"request_id":"cli-record-1"}'
```

On Windows, `watch_gegi_topics.ps1` opens one Prism CLI watcher per selected text topic. It can run `prism` on the host or inside a running GeGi container; use `Get-Help ./watch_gegi_topics.ps1 -Detailed` or inspect its parameters for target and connection overrides.

## Recorder output

For each timed recording, `data_recorder_node.py` writes timestamp-prefixed assets under `--output-dir`, including:

- `*_compton_events.jsonl`: one raw Compton-event JSON object per line.
- `*_spectrum.n42`: ANSI N42.42 spectrum data.
- `*_activity.csv`: whole-run detected-line activity summary aligned with the N42 (not per-window measurements; see the migration notes).
- `*_heatmap_raw.csv`, `*_heatmap_raster.csv`, and `*_heatmap_3d.csv`: imaging products.
- `*_manifest.json`: measurement identity, configuration, and generated-asset index.

## Live plotting

The live plotting tools connect directly to Prism/NATS; no websocket bridge is involved. Run them on any host that can reach NATS and has the Prism Python bindings plus plotting dependencies installed:

```bash
python3 tools/plot_live_spectrum.py --protocol nats --server localhost --port 4222
python3 tools/plot_live_heatmap.py --protocol nats --server localhost --port 4222
python3 tools/plot_live_2d_heatmap.py --protocol nats --server localhost --port 4222
```

## Tests

```bash
bash test/run_tests.sh
```

The tests do not require detector hardware or NATS, but the selected Python 3 environment must have the Prism bindings and the processing dependencies importable.

## License

See [LICENSE.md](LICENSE.md).
