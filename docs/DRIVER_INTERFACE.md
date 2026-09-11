# GeGI Driver — Messaging & Integration Interface (legacy ROS baseline)

> **Historical document:** this records the pre-migration ROS 1 interface that
> was used as the source inventory. It is no longer the runtime contract. See
> [`PRISM_MIGRATION.md`](PRISM_MIGRATION.md) for the implemented Prism topics,
> commands, wire schemas, build, and integration instructions.

Handover reference for integrating this driver into PRISM. It describes the
messaging system, the full topic/service surface, message definitions, and the
two ways an external system can talk to it.

## 1. Messaging model at a glance
There are **two messaging layers**:

```
 GeGI detector  <--(A) raw TCP binary-->  C++ driver node  <--(B) ROS 1 pub/sub + services-->  consumers / PRISM
 192.168.50.109:27015                     phds_gegi_driver                                       (native ROS or rosbridge)
```

- **(A) Detector ↔ driver — raw TCP, binary GeGI protocol.** A single TCP socket
  to the detector (default `192.168.50.109:27015`). Variable-length event packets
  stream in; single-byte commands go out (`g`=start, `s`=stop, `c`=clear,
  `i`=run-info, `d`=detector-info, `1`–`8`=timed presets, …). This is internal to
  the C++ node — **PRISM does not touch this layer.**
- **(B) Driver ↔ everything else — ROS 1 (Melodic) middleware.** This is the
  integration surface. Standard ROS 1 transport (**TCPROS**: an XMLRPC master
  `roscore` for discovery, then a direct TCP connection per topic). All data and
  control are ROS **topics** (pub/sub) and **services** (request/response).
- **Non-ROS bridge:** a **rosbridge websocket server on port 9090** is launched
  with the pipeline. Any language can subscribe/publish/call services as **JSON
  over websocket** without linking ROS. The existing Windows plotting tools in
  `tools/` use exactly this.

**Stack:** ROS 1 Melodic, catkin. The detector node is C++; the processing nodes
(spectrum, activity, heatmap, recorder) are Python 2.7.

## 2. Node graph & data flow
| Node | Language | Consumes | Produces |
|------|----------|----------|----------|
| `phds_gegi_driver` | C++ | detector TCP stream | `/compton_event`, `/energy_deposit`, `/energy_deposit_singles`; detector control services |
| `spectrum_node` | Py | `/energy_deposit` | `/spectrum` |
| `activity_node` | Py | `/spectrum` | `/activity/results`, `/activity/total_activity`, `/activity/effective_source_distance` |
| `spherical_heatmap_node` | Py | `/compton_event`, `/activity/results` | `/sphere_heatmap`, `/source_direction(s)`, `/source_isotopes` |
| `data_recorder_node` | Py | all of the above | files (N42, CSV, rosbag, manifest); recording services |

## 3. Topics (the data surface)
| Topic | Type | Dir | Notes |
|-------|------|-----|-------|
| `/compton_event` | `radiation_detector_msgs/ComptonEvent` | out | one msg per 2-site Compton event (imaging input) |
| `/energy_deposit` | `std_msgs/Float64` | out | per-event energy (keV); 1-site + 2-site-summed → spectrum |
| `/energy_deposit_singles` | `std_msgs/Float64` | out | per-event energy (keV), 1-site only |
| `/spectrum` | `radiation_detector_msgs/Spectrum` | out | histogram, republished ~4 Hz |
| `/activity/results` | `std_msgs/String` | out | **JSON** activity report (see §5) |
| `/activity/total_activity` | `std_msgs/Float64` | out | summed activity (MBq) |
| `/activity/effective_source_distance` | `std_msgs/Float64` | out | latched; plate-derived standoff (m) |
| `/sphere_heatmap` | `sensor_msgs/PointCloud2` | out | back-projection image (xyz + per-isotope dose fields) |
| `/source_direction` | `geometry_msgs/PoseStamped` | out | strongest source direction |
| `/source_directions` | `geometry_msgs/PoseArray` | out | all detected source directions |
| `/source_isotopes` | `std_msgs/String` | out | `"Cs-137:count|Co-60:count"` |
| `/activity/n_shielding_plates` | `std_msgs/Int32` | **in** | operator sets extra shielding plates |
| `/detector/dead_time_percent` | `std_msgs/Float64` | out | optional shared dead-time (off by default) |

Note on rates: `/compton_event` and `/energy_deposit` fire once **per detector
event** and can burst to thousands/sec. Publisher and subscriber queues are sized
at 10,000 so bursts are not dropped.

## 4. Services (the control surface)
Detector control (provided by the C++ node, under `/detector/`):

| Service | Type | Purpose |
|---------|------|---------|
| `/detector/start_acquisition` | `phds_gegi_driver/StartAcquisition` | begin free-running acquisition |
| `/detector/stop_acquisition` | `phds_gegi_driver/StopAcquisition` | stop |
| `/detector/clear_data` | `phds_gegi_driver/ClearData` | clear detector spectrum/state |
| `/detector/start_timed_acquisition` | `phds_gegi_driver/StartTimedAcquisition` | detector-timed preset run |
| `/detector/get_run_info` | `phds_gegi_driver/GetRunInfo` | real/live time, dead-time %, count rate |
| `/detector/get_detector_info` | `phds_gegi_driver/GetDetectorInfo` | serial, temperature, bias, battery |
| `/detector/toggle_bias_mode` | `phds_gegi_driver/ToggleBiasMode` | HV bias on/off |

Recording / orchestration (provided by `data_recorder_node`, under `/data_recorder/`):

| Service | Type | Purpose |
|---------|------|---------|
| `/data_recorder/start_timed_recording` | `phds_gegi_driver/StartTimedAcquisition` | run + save all assets for N minutes |
| `/data_recorder/stop_recording` | `std_srvs/Trigger` | stop early |
| `/data_recorder/clear_all` | `std_srvs/Trigger` | clear detector + all node state |

Each processing node also offers `~clear` (and the heatmap `~save_csv`) as
`std_srvs/Trigger`.

## 5. Message definitions
**`radiation_detector_msgs/ComptonEvent`**
```
std_msgs/Header header
float64 energy_kev_1
geometry_msgs/Point reading_location_1   # metres, ROS REP-105 frame
float64 energy_kev_2
geometry_msgs/Point reading_location_2
float64 cone_angle
float64 cone_angle_uncertainty
```

**`radiation_detector_msgs/Spectrum`**
```
std_msgs/Header header
time   endTime
uint32 realTime_s        # legacy
uint32 realTime_ms       # use this
uint32 deadTime_ms
uint32 totalCount
uint32[] spectrum        # counts per energy bin
```

**`phds_gegi_driver/GetRunInfo`** (request empty)
```
---
bool    success
float64 real_time_sec
float64 live_time_sec
float64 dead_time_percent
float64 count_rate_hz
string  message
```

**`phds_gegi_driver/StartTimedAcquisition`**
```
int32 duration_minutes
---
bool   success
string message
```

**`/activity/results` (JSON in `std_msgs/String`)** — top-level object with a
`timestamp`, `total_activity_MBq`, and an `isotopes` array; each element carries
`isotope`, `energy_keV`, `activity_MBq`, `sigma_activity_MBq`, `valid`,
`net_corrected`, `source_distance_m`, `total_shield_plates`, `shield_transmission`,
`method`, etc. This is the easiest single feed for PRISM to consume — parse the
JSON string, no ROS message compilation needed.

## 6. Message packages / build dependencies
- **`radiation_detector_msgs`** — external msg package (in `deps/`): `ComptonEvent`,
  `Spectrum`, `GetSpectrum`, … PRISM needs these definitions to deserialize the
  imaging/spectrum topics natively (not needed via rosbridge JSON).
- **`phds_gegi_driver/srv`** — this package's custom services (§4).
- Standard: `std_msgs`, `geometry_msgs`, `sensor_msgs`, `std_srvs`.

## 7. Integrating with PRISM — two paths
**A. Native ROS 1 node (recommended if PRISM can host or reach a ROS master).**
Point PRISM at the `ROS_MASTER_URI`, build against `radiation_detector_msgs` +
`phds_gegi_driver`, then subscribe to the topics and call the services directly.
Lowest latency, full-rate event access.

**B. rosbridge websocket (recommended if PRISM is non-ROS / cross-platform).**
Connect to `ws://<host>:9090` and exchange JSON (`op: subscribe/publish/call_service`).
No ROS build dependency; works from any language. Already running in the pipeline
(`gegi_full_pipeline.launch`) and used by the `tools/plot_live_*.py` clients as
reference implementations. Caveat: JSON encoding of high-rate `/compton_event`
and binary `PointCloud2` is heavier — for bulk event throughput prefer path A,
and use rosbridge for the digested feeds (`/activity/results`, `/spectrum`,
`/source_isotopes`, dose).

## 8. Practical notes for the integrator
- The detector allows **one TCP connection**; the C++ node owns it. Do not open a
  second connection to the detector — go through the ROS interface.
- `/activity/results` and `/source_isotopes` are the highest-value digested feeds
  for a supervisory system; raw `/compton_event` is only needed for custom
  imaging.
- `/activity/effective_source_distance` is **latched** — a late subscriber still
  receives the last value.
- Control flow for a measurement: call `/data_recorder/start_timed_recording`
  (it orchestrates the detector run and writes all assets), or drive the detector
  directly via `/detector/*` if PRISM manages its own acquisition lifecycle.
- Everything is ROS 1 Melodic; there is no ROS 2 / DDS layer in this driver.
