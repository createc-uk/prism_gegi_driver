#!/usr/bin/env python3
"""Isolated, non-hardware NATS one-shot CLI delivery regression test.

Start a standalone NATS server at 127.0.0.1:14222 before running.
Publishes only to a unique diagnostic subject, never GeGi detector topics.
Requires nats-py on the host. PRISM_BIN may select another Prism CLI binary.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


URL = os.environ.get("PROBE_NATS_URL", "nats://127.0.0.1:14222")
PORT = URL.rsplit(":", 1)[-1]
HOST = URL.split("://", 1)[-1].rsplit(":", 1)[0]
CLI = os.environ.get("PRISM_BIN", str(Path(__file__).resolve().parents[1] / "build/bin/prism"))
N = int(os.environ.get("PROBE_COUNT", "100"))
SUBJECT = "gegi.diagnostics.prism_delivery_" + uuid.uuid4().hex[:15]


async def main():
    import nats
    c = await nats.connect(URL, connect_timeout=5)
    observed = []
    async def on_message(msg):
        try:
            observed.append(json.loads(msg.data.decode()).get("request_id"))
        except Exception:
            observed.append("unparsable")
    sub = await c.subscribe(SUBJECT, cb=on_message)
    await c.flush(timeout=5)
    pub_errors = []
    start = time.monotonic()
    for i in range(N):
        payload = json.dumps({"dataType": "Command", "command": "diagnostic_noop", "request_id": f"test-{i}"})
        r = await asyncio.create_subprocess_exec(
            CLI, "pub", "--protocol", "nats", "--ip", HOST, "--port", PORT,
            "--topic", SUBJECT, "--message", payload,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await r.communicate()
        if r.returncode != 0:
            pub_errors.append((i, r.returncode, err.decode(errors="replace")[:160]))
        if (i+1)%50==0:
            print(f"sent={i+1}, received={len(observed)}, errors={len(pub_errors)}",flush=True)
    await asyncio.sleep(1)
    missing=sorted(set(f"test-{i}" for i in range(N))-set(observed))
    print(f"RESULT: sent={N}, received={len(observed)}, missing={len(missing)}, failures={len(pub_errors)}, elapsed={time.monotonic()-start:.2f}s", flush=True)
    if missing: print("MISSING IDs:", missing[:30], flush=True)
    if pub_errors: print("CLI ERRORS:",pub_errors[:5],flush=True)
    await sub.unsubscribe()
    await c.close()
    return 0 if not pub_errors and not missing else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
