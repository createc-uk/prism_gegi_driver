# Historical upstream ROS 1 files (not part of the Prism runtime)

This archive preserves the unmerged ROS-facing additions from the upstream
`aliyu-createc/gegi_driver` repository's `main` and `devel` branches during the
2026-11-12 consolidation. Its lineage is also recorded in the Git merge commit.

- `DRIVER_INTERFACE_ROS.md` — upstream's latest ROS 1 interface/health document.
- `launch/` — ROS Melodic/catkin launch files from upstream.
- `src/phds_gegi_driver.cpp` — upstream ROS C++ driver as of `devel`.
- `wsl/` — upstream's ROS Melodic WSL setup instructions and script.
- `probe_detector.py` — upstream diagnostic tool; it may open a detector TCP
  connection and MUST NOT be used concurrently with the Prism driver.

These files are NOT installed by the Prism Docker image, NOT compiled by the
Prism CMake project, and NOT the supported runtime interface. Use
`../PRISM_MIGRATION.md`, the root `README.md`, and the Prism topic/command
contracts for active deployments.

The important upstream `devel` behaviour was ported to the active Prism C++
components: automatic single-socket TCP reconnection and the
`gegi.detector.health` heartbeat. It does **not** replay detector commands or
resume an interrupted acquisition after reconnection.
