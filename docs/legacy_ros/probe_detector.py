#!/usr/bin/env python3
"""Standalone GeGI connectivity probe - no ROS required.

Opens the single allowed TCP connection to the detector, sends the two
READ-ONLY status commands ('d' = detector info, 'i' = run info), prints the
raw replies, and tries to decode the 28-byte detector-info frame exactly as
the C++ driver does (src/phds_gegi_driver/socket_comms/tcp_event_reader.cpp).
Starts no acquisition and changes no detector state.

Usage:
    python3 tools/probe_detector.py [ip] [port]     # default 172.31.1.40 27015

NOTE: the detector accepts ONE TCP connection. Do not run this while the
driver node is connected.
"""
import socket
import struct
import sys
import time

DEFAULT_HOST = "172.31.1.40"
DEFAULT_PORT = 27015


def recv_for(sock, seconds, max_bytes=16384):
    sock.setblocking(False)
    buf = b""
    deadline = time.time() + seconds
    while time.time() < deadline and len(buf) < max_bytes:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                print("  (peer closed connection)")
                break
            buf += chunk
        except (BlockingIOError, socket.timeout):
            time.sleep(0.05)
    return buf


def decode_detector_info(b):
    """Scan for the 28-byte frame: char[4] serial, f64 temp_K, i32 bias,
    i32 line_power, i32 batt1, i32 batt2 (little-endian), using the same
    plausibility checks as the C++ driver."""
    for i in range(0, max(0, len(b) - 27)):
        serial = b[i:i + 4].split(b"\0")[0]
        try:
            txt = serial.decode("ascii")
        except UnicodeDecodeError:
            continue
        if not txt or not txt[0].isalpha() or not all(c.isalnum() or c == "-" for c in txt):
            continue
        temp, bias, line, b1, b2 = struct.unpack_from("<diiii", b, i + 4)
        if not (-50.0 <= temp <= 200.0):
            continue
        if bias not in (0, 1) or line not in (0, 1):
            continue
        if not (0 <= b1 <= 100 and 0 <= b2 <= 100):
            continue
        return dict(offset=i, serial=txt, temp_K=round(temp, 2), bias=bias,
                    line_power=line, batt1_pct=b1, batt2_pct=b2)
    return None


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    print("Connecting to %s:%d ..." % (host, port))
    s = socket.create_connection((host, port), timeout=5)
    print("Connected.")

    # Unprompted traffic would mean an acquisition is already streaming, or
    # the port is some other service (e.g. a TLS/HTTP banner on 8443).
    idle = recv_for(s, 1.5)
    print("\nUnprompted bytes in 1.5 s: %d" % len(idle))
    if idle:
        print("  first 64 hex:", idle[:64].hex())

    for cmd in (b"d", b"i"):
        print("\nSending %r ..." % cmd)
        s.sendall(cmd)
        buf = recv_for(s, 3.0)
        print("  reply: %d bytes" % len(buf))
        if buf:
            print("  first 64 hex:", buf[:64].hex())
        if cmd == b"d" and buf:
            info = decode_detector_info(buf)
            if info:
                print("  DECODED DETECTOR INFO:", info)
            else:
                print("  (no plausible detector-info frame found - wrong port?)")

    s.close()
    print("\nDone. Connection closed; no acquisition was started.")


if __name__ == "__main__":
    main()
