#!/usr/bin/env python3
"""Software-only GeGi -> Prism/NATS -> N42 integration smoke test.

This binds a fake detector to 127.0.0.1 and never contacts actual detector
hardware. Requires locally installed nats-server, Prism Python bindings,
the built C++ driver, and the existing Prism CLI. Opt-in; not unittest discover.

Usage: python3 test/simulated_pipeline_smoke.py
       python3 test/simulated_pipeline_smoke.py --docker

--docker runs the deployed container image, with host networking used only
for the locally-bound NATS/mock TCP sockets on a Linux development machine.
"""
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "build" / "phds_gegi_driver_node"
PRISM = ROOT / "build" / "bin" / "prism"
LAUNCHER = ROOT / "scripts" / "run_full_pipeline.sh"


def unused_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def can_connect(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def wait_for(predicate, seconds, what, pipeline=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        if pipeline is not None and pipeline.poll() is not None:
            raise RuntimeError(f"Pipeline exited early ({pipeline.returncode}) waiting for {what}")
        time.sleep(0.12)
    raise RuntimeError(f"Timeout waiting for {what}")


class MockGeGi:
    def __init__(self, host="127.0.0.1", port=0):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(1)
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.accepted = threading.Event()
        self.streaming = threading.Event()
        self.commands = []
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.start_time = None
        self.conn = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.conn:
            with contextlib.suppress(Exception):
                self.conn.shutdown(socket.SHUT_RDWR)
                self.conn.close()
        self.sock.close()
        self.thread.join(timeout=2)

    def send(self, payload):
        with self.write_lock:
            if self.conn:
                self.conn.sendall(payload)

    def events(self):
        seq = 0
        while not self.stop.wait(0.035):
            if not self.streaming.is_set():
                continue
            seq += 1
            if seq % 3:
                # 92-byte Compton event: 4-byte type, 8-byte time, 10 doubles.
                frame = struct.pack("<IQ10d", 2, seq, 0.0, 0.0, 0.0,
                                    250.0, 5.0, 3.0, 0.0, 411.7, 1.1, 0.02)
            else:
                frame = struct.pack("<IQ4d", 0, seq, 0.0, 0.0, 0.0, 661.7)
            try:
                self.send(frame)
            except OSError:
                return

    def serve(self):
        conn = None
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
                break
            except socket.timeout:
                continue
            except OSError:
                return
        if conn is None:
            return
        self.conn = conn
        self.accepted.set()
        conn.settimeout(0.2)
        event_thread = threading.Thread(target=self.events, daemon=True)
        event_thread.start()
        try:
            while not self.stop.is_set():
                try:
                    command = conn.recv(256)
                except socket.timeout:
                    continue
                if not command:
                    return
                for byte in command:
                    cmd = chr(byte)
                    with self.lock:
                        self.commands.append(cmd)
                    if cmd in "12345678g":
                        self.start_time = time.monotonic()
                        self.streaming.set()
                    elif cmd == "s":
                        self.streaming.clear()
                    elif cmd in "xc":
                        self.streaming.clear()
                        self.start_time = None
                    elif cmd == "i":
                        if self.start_time is None:
                            # A complete 28-byte idle reply, with NO event
                            # following, tests the short-frame parser.
                            self.send(struct.pack("<Iddd", 0, 0.0, 0.0, 0.0))
                        else:
                            real = max(1, int(time.monotonic() - self.start_time))
                            self.send(struct.pack("<Iddd", real, real * 0.98, 0.0, 200.0))
                    elif cmd == "d":
                        self.send(struct.pack("<4sd4i", b"G596", 92.0,
                                              1, 1, 85, 84))
        finally:
            self.stop.set()
            event_thread.join(timeout=2)


def terminate(proc, group=False):
    if proc is None or proc.poll() is not None:
        return
    try:
        if group:
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if group:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=3)


def publish(topic, command, port, container_name=None):
    # In Docker mode, use the Prism CLI bundled IN the image. The host
    # therefore only needs Docker and Python to run this smoke test.
    prefix = ["docker", "exec", container_name, "prism"] if container_name else [str(PRISM)]
    args = prefix + ["pub", "--protocol", "nats", "--ip",
            "127.0.0.1", "--port", str(port), "--topic", topic,
            "--message", json.dumps(command, separators=(",", ":"))]
    run = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                         timeout=8)
    if run.returncode:
        raise RuntimeError(f"prism pub failed: {run.stderr}\n{run.stdout}")


def main():
    docker_mode = '--docker' in sys.argv[1:]
    if not docker_mode and (not PRISM.is_file() or not DRIVER.is_file()):
        raise RuntimeError("Build the local Prism CLI and native driver for local mode")
    if docker_mode:
        subprocess.run(["docker", "image", "inspect", "prism-gegi-driver:latest"],
                       capture_output=True, text=True, check=True)
    nats_port = unused_port()
    with tempfile.TemporaryDirectory(prefix="gegi-sim-smoke-") as td:
        data = Path(td) / "data"
        data.mkdir()
        nats_log_path = Path(td) / "nats.log"
        pipeline_log_path = Path(td) / "pipeline.log"
        nats = pipeline = None
        container_name = "gegi-docker-smoke-{}".format(os.getpid())
        nats_container_name = "gegi-docker-nats-smoke-{}".format(os.getpid())
        with MockGeGi() as sim, open(nats_log_path, "w") as nlog, open(
                pipeline_log_path, "w") as plog:
            try:
                if docker_mode:
                    nats_command = [
                        "docker", "run", "--rm", "--network", "host",
                        "--name", nats_container_name, "nats:2-alpine",
                        "-a", "127.0.0.1", "-p", str(nats_port)]
                else:
                    nats_command = ["nats-server", "-a", "127.0.0.1",
                                    "-p", str(nats_port)]
                nats = subprocess.Popen(
                    nats_command, stdout=nlog, stderr=subprocess.STDOUT, cwd=ROOT)
                wait_for(lambda: can_connect("127.0.0.1", nats_port),
                         10, "NATS listener", nats)

                env = dict(os.environ)
                env.update(
                    PRISM_PROTOCOL="nats", PRISM_SERVER="127.0.0.1",
                    PRISM_PORT=str(nats_port), DETECTOR_IP="127.0.0.1",
                    DETECTOR_PORT=str(sim.port), OUTPUT_DIR=str(data),
                    # The isolated Control worker is limited to 128 tasks;
                    # NumPy/OpenBLAS can otherwise allocate many idle worker
                    # threads per process and exhaust the entire job cgroup.
                    OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                    MKL_NUM_THREADS="1")
                if docker_mode:
                    env_to_pass = (
                        "PRISM_PROTOCOL", "PRISM_SERVER", "PRISM_PORT",
                        "DETECTOR_IP", "DETECTOR_PORT",
                        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                        "MKL_NUM_THREADS")
                    command = [
                        "docker", "run", "--rm", "--init",
                        "--network", "host", "--name", container_name,
                        "-v", "{}:/opt/phds_gegi_driver/data".format(data),
                    ]
                    for name in env_to_pass:
                        command.extend(["-e", "{}={}".format(name, env[name])])
                    command.append("prism-gegi-driver:latest")
                else:
                    command = ["bash", str(LAUNCHER)]
                pipeline = subprocess.Popen(
                    command, stdout=plog, stderr=subprocess.STDOUT,
                    env=env, cwd=ROOT, start_new_session=True)
                wait_for(sim.accepted.is_set, 18 if docker_mode else 8,
                         "simulated TCP connection", pipeline)
                # Wait for recorder and downstream subscriptions to start.
                time.sleep(5.0)
                with sim.lock:
                    initial = tuple(sim.commands)
                if "i" not in initial:
                    raise RuntimeError(f"Driver never requested run-info: {initial}")
                # Detector-info polling is best effort and can run after the
                # first run-info request. Do not assert exact startup ordering.
                publish("gegi.data_recorder.command", {
                    "command": "start_timed_recording", "duration_minutes": 5,
                    "request_id": "smoke-start-01"}, nats_port,
                        container_name if docker_mode else None)
                wait_for(lambda: "1" in sim.commands, 16,
                         "deep clear then start timed acquisition", pipeline)
                with sim.lock:
                    commands = list(sim.commands)
                if "x" not in commands or commands.index("x") > commands.index("1"):
                    raise RuntimeError(f"No pre-acquisition deep clear: {commands}")
                time.sleep(7.0)
                publish("gegi.data_recorder.command", {
                    "command": "stop_recording",
                    "request_id": "smoke-stop-01"}, nats_port,
                        container_name if docker_mode else None)
                wait_for(lambda: bool(list(data.glob("*_manifest.json"))), 30,
                         "N42/CSV/manifest output", pipeline)
                n42 = list(data.glob("*.n42"))
                events = list(data.glob("*_compton_events.jsonl"))
                activity = list(data.glob("*_activity.csv"))
                if not n42 or not events or not activity:
                    raise RuntimeError(f"Missing expected output: {list(data.iterdir())}")
                if not any(p.stat().st_size > 0 for p in events):
                    raise RuntimeError("Simulated Compton events were not recorded")
                xml = ET.parse(n42[0])
                channels = xml.find(".//{*}ChannelData")
                if channels is None or not channels.text:
                    raise RuntimeError("N42 spectrum lacks ChannelData")
                total_counts = sum(int(x) for x in channels.text.split())
                if total_counts <= 0:
                    raise RuntimeError("N42 contains no simulated spectrum counts")
                manifest = json.loads(next(data.glob("*_manifest.json")).read_text())
                if not manifest.get("measurement_id"):
                    raise RuntimeError("Manifest does not carry measurement identity")
                asset_files = {a["file"] for a in manifest.get("assets", [])}
                if not {n42[0].name, events[0].name, activity[0].name}.issubset(asset_files):
                    raise RuntimeError(f"Manifest missing assay evidence: {asset_files}")
                with sim.lock:
                    final_commands = list(sim.commands)
                if "s" not in final_commands:
                    raise RuntimeError("Recorder did not forward stop_acquisition")
                logs = pipeline_log_path.read_text(errors="replace")
                if ("Run Info [inline]" not in logs
                        or "Detector Info [inline]" not in logs):
                    raise RuntimeError("Idle/status inline response decoding was not exercised")
                if "Traceback" in logs:
                    raise RuntimeError("A pipeline node emitted a Python traceback")
                # A write-side acknowledgement is not a physical detector ACK;
                # this test validates only the software path, not real hardware.
                print("PASS: {} pipeline, isolated NATS and fake TCP GeGi".format(
                    "Docker container" if docker_mode else "native six-node"))
                print("PASS: full clear preceded timed start, followed by early stop")
                print("PASS: Compton JSONL, N42, activity CSV, measurement manifest")
                print(f"Detected commands: {''.join(final_commands)}")
                print(f"N42 simulated total: {total_counts} counts")
                print("Output:", ", ".join(sorted(p.name for p in data.iterdir())))
            except Exception:
                plog.flush()
                print("=== condensed pipeline error context ===")
                try:
                    lines = pipeline_log_path.read_text(errors="replace").splitlines()
                    markers = ("Traceback", "RuntimeError", "Resource temporarily",
                               "Error:", "ModuleNotFoundError", "failed with status",
                               "Stopping GeGi pipeline", "Exception")
                    indices = {i for i, line in enumerate(lines)
                               if any(marker in line for marker in markers)}
                    chosen = set(range(min(45, len(lines))))
                    chosen.update(range(max(0, len(lines) - 45), len(lines)))
                    for i in indices:
                        chosen.update(range(max(0, i - 6), min(len(lines), i + 8)))
                    print("\n".join(f"{i + 1}: {lines[i]}" for i in sorted(chosen)))
                except OSError:
                    pass
                raise
            finally:
                if docker_mode:
                    subprocess.run(["docker", "stop", "--time", "5", container_name],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15)
                terminate(pipeline, group=True)
                if docker_mode:
                    subprocess.run(["docker", "rm", "-f", container_name],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15)
                if docker_mode:
                    subprocess.run(["docker", "stop", "--time", "5", nats_container_name],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15)
                terminate(nats)
                if docker_mode:
                    subprocess.run(["docker", "rm", "-f", nats_container_name],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15)


if __name__ == "__main__":
    if "--mock-server" in sys.argv:
        # Used by the Compose-only verification override on an isolated
        # private Docker network. Never publish the fake detector's port.
        with MockGeGi(host="0.0.0.0", port=27015) as simulator:
            print("Mock GeGi ready on TCP port 27015", flush=True)
            simulator.thread.join()
    else:
        main()
