#!/usr/bin/env python3
"""Hardware-free Prism CLI -> NATS -> CommandServer -> NATS result proof.

Prereqs: isolated NATS on 127.0.0.1:14222 and mock_prism_command_endpoint.py.
Uses synthetic gegi.diagnostics.command only; never contacts GeGi or ImagerPro.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid


PRISM_BIN = os.environ.get("PRISM_BIN",
    str(Path(__file__).resolve().parents[1] / "build/bin/prism"))
N = int(os.environ.get("PROBE_COUNT","80"))
PORT = os.environ.get("PROBE_PORT","14222")
CMD = "gegi.diagnostics.command"


async def main():
    import nats
    nc = await nats.connect(f"nats://127.0.0.1:{PORT}")
    responses = {}
    async def receive(msg):
        obj = json.loads(msg.data.decode())
        responses.setdefault(obj.get('request_id'), []).append(obj)
    sub = await nc.subscribe(CMD+'_result', cb=receive)
    await nc.flush(timeout=5)

    async def publish(message):
        proc = await asyncio.create_subprocess_exec(
            PRISM_BIN, 'pub', '--protocol', 'nats', '--ip', '127.0.0.1',
            '--port', PORT, '--topic', CMD, '--message', json.dumps(message),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f'CLI send failed: {err.decode(errors="replace")[:200]}')

    unique = uuid.uuid4().hex[:10]
    first=time.monotonic()
    for i in range(N):
        await publish({"dataType":"Command","command":"diagnostic_ping",
                       "request_id":f"probe-{unique}-{i}"})
    deadline=time.monotonic()+5
    while sum(1 for i in range(N) if f"probe-{unique}-{i}" in responses)<N and time.monotonic()<deadline:
        await asyncio.sleep(.05)
    received=sum(1 for i in range(N) if f"probe-{unique}-{i}" in responses)
    print(f"Initial command requests: sent={N} acknowledged={received} elapsed={time.monotonic()-first:.2f}s",flush=True)
    assert received==N, f"Lost {N-received} command replies"
    assert all(responses[f"probe-{unique}-{i}"][0]["success"] for i in range(N))

    dup=f"probe-{unique}-duplicate"
    await publish({"command":"diagnostic_ping","request_id":dup})
    await publish({"dataType":"Command","command":"diagnostic_ping","request_id":dup})
    deadline=time.monotonic()+2
    while len(responses.get(dup,[]))<2 and time.monotonic()<deadline:
        await asyncio.sleep(.05)
    assert len(responses.get(dup,[]))==2, "Duplicated request did not get both replies"
    assert responses[dup][0]==responses[dup][1], "Duplicate executed again instead of returning cached reply"
    print("Duplicate command returned cached, identical acknowledgement: PASS",flush=True)

    invalid=f"probe-{unique}-bad"
    await publish({"command":None, "request_id":invalid})
    deadline=time.monotonic()+2
    while invalid not in responses and time.monotonic()<deadline:
        await asyncio.sleep(.05)
    assert invalid in responses, "Malformed command received no failure reply"
    assert responses[invalid][0]['success'] is False
    print("Malformed command returned explicit failure acknowledgement: PASS",flush=True)
    await sub.unsubscribe()
    await nc.close()
    print("PASS: isolated NATS -> Python Prism CommandServer roundtrip",flush=True)


if __name__ == '__main__':
    asyncio.run(main())
