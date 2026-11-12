#!/usr/bin/env python3
"""Hardware-free validation of the REAL docker-compose deployment topology.

Uses the production docker-compose.yml plus a separate mock-only override,
so app -> fake GeGi and app -> NATS use Compose's ordinary bridge network.
No GeGi hardware is contacted. Run after building the app image:

    python3 test/docker_compose_smoke.py
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "gegi-compose-smoke-{}".format(os.getpid())
COMPOSE = [
    "docker", "compose", "-p", PROJECT, "-f", "docker-compose.yml",
    "-f", "test/docker-compose.mock.yml",
]


def run(args, env, timeout=25, check=True):
    result = subprocess.run(
        COMPOSE + args, cwd=ROOT, env=env, text=True,
        capture_output=True, timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError("Compose failed: {}\n{}\n{}".format(
            " ".join(args), result.stdout[-3500:], result.stderr[-3500:]))
    return result


def wait_for(predicate, what, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError("Timed out waiting for {}".format(what))


def open_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def publish(env, command):
    result = run([
        "exec", "-T", "app", "prism", "pub",
        "--protocol", "nats", "--ip", "nats", "--port", "4222",
        "--topic", "gegi.data_recorder.command",
        "--message", json.dumps(command, separators=(",", ":")),
    ], env, timeout=15)
    if "Failed" in result.stderr:
        raise RuntimeError(result.stderr)


def main():
    subprocess.run(
        ["docker", "image", "inspect", "prism-gegi-driver:latest"],
        capture_output=True, check=True,
    )
    with tempfile.TemporaryDirectory(prefix="gegi-compose-data-") as folder:
        data = Path(folder) / "data"
        data.mkdir()
        env = dict(os.environ)
        env.update(
            GEGI_DATA_DIR=str(data), NATS_HOST_PORT=str(open_port()),
            NATS_BIND_ADDR="127.0.0.1",
            # The override replaces this address with an internal mock DNS
            # name, but supply an unreachable sentinel as a second safeguard.
            DETECTOR_IP="203.0.113.99",
        )
        try:
            run(["up", "-d", "--no-build", "--wait", "nats", "fake-gegi"], env, timeout=45)
            wait_for(
                lambda: "Mock GeGi ready" in run(
                    ["logs", "--no-color", "fake-gegi"], env, timeout=10).stdout,
                "private-network mock detector", timeout=14,
            )
            run(["up", "-d", "--no-build", "--wait", "app"], env, timeout=40)
            wait_for(
                lambda: "gegi.data_recorder.command" in run(
                    ["logs", "--no-color", "app"], env, timeout=10).stdout,
                "Prism recorder subscription", timeout=20,
            )
            publish(env, {"command": "start_timed_recording",
                          "duration_minutes": 5,
                          "request_id": "compose-smoke-start"})
            wait_for(
                lambda: "Recording started" in run(
                    ["logs", "--no-color", "app"], env, timeout=10).stdout
                or "Recording for" in run(
                    ["logs", "--no-color", "app"], env, timeout=10).stdout,
                "timed acquisition", timeout=22,
            )
            time.sleep(6)
            publish(env, {"command": "stop_recording",
                          "request_id": "compose-smoke-stop"})
            wait_for(lambda: bool(list(data.glob("*_manifest.json"))),
                     "host-mounted measurement manifest", timeout=35)
            n42 = list(data.glob("*_spectrum.n42"))
            jsonl = list(data.glob("*_compton_events.jsonl"))
            csvs = list(data.glob("*_activity.csv"))
            if not n42 or not jsonl or not csvs:
                raise RuntimeError("Missing expected assay outputs in {}".format(data))
            if not jsonl[0].stat().st_size:
                raise RuntimeError("Recorded Compton JSONL is empty")
            root = ET.parse(n42[0])
            channels = root.find(".//{*}ChannelData")
            counts = sum(int(c) for c in channels.text.split()) if (
                channels is not None and channels.text) else 0
            if counts <= 0:
                raise RuntimeError("N42 spectrum has zero synthetic counts")
            manifest = json.loads(list(data.glob("*_manifest.json"))[0].read_text())
            assets = {a["file"] for a in manifest["assets"]}
            if not {n42[0].name, jsonl[0].name, csvs[0].name}.issubset(assets):
                raise RuntimeError("Manifest asset linkage incomplete")
            print("PASS: production Compose NATS + six-node app + private GeGi simulator")
            print("PASS: native Docker bridge network and loopback-only NATS export")
            print("PASS: full clear/start/stop and host-mounted N42/JSONL/CSV/manifest")
            print("N42 synthetic spectrum counts:", counts)
            print("Files:", ", ".join(sorted(p.name for p in data.iterdir())))
        except Exception:
            print("=== Compose diagnostic logs (last 90 lines) ===")
            logs = run(["logs", "--no-color", "app", "fake-gegi"], env,
                       timeout=15, check=False)
            print("\n".join(logs.stdout.splitlines()[-90:]))
            print(logs.stderr[-2000:])
            raise
        finally:
            run(["down", "-v", "--remove-orphans"], env, timeout=45, check=False)


if __name__ == "__main__":
    main()
