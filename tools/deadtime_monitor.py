#!/usr/bin/env python
"""Live dead-time / count-rate monitor for DJR Assay Experiment C.

The GeGI's run-info dead_time_percent is CUMULATIVE since acquisition start,
so it decays as a fixed start-up dead period is diluted over elapsed time (e.g.
~24% at 2 s -> ~1.7% at 28 s) and does NOT reflect the true loading. This tool
subscribes to the detector's periodically-published run-info topic and
computes the INSTANTANEOUS dead-time from the change in real/live time
between messages, which is the correct observable for the rate/throughput
characterisation:

    instantaneous DT = (1 - d_live/d_real) * 100

The C++ driver publishes run-info as JSON on --topic (default
gegi.detector.run_info) periodically -- every 2 s while idle, every 30 s while
an acquisition is active -- rather than serving it on demand, so this tool is
a passive subscriber: it processes and prints a line for every message it
receives (whatever the arrival cadence happens to be).

Run it while stepping the source closer; read the steady instantaneous DT and
count rate at each standoff. Optionally log to CSV.

  python tools/deadtime_monitor.py
  python tools/deadtime_monitor.py --protocol nats --server localhost --port 4222 \\
      --csv "data/Experiment C/deadtime_log.csv"
"""
from __future__ import print_function

import json
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "src", "phds_gegi_driver"))

import prism

import prism_messages as pmsg


def main():
    app = prism.Application("deadtime_monitor", "GeGI instantaneous dead-time monitor", sys.argv)

    app.add_string_option("DeadtimeMonitor", "topic",
                           "Run-info topic published periodically by the detector driver",
                           "gegi.detector.run_info")
    app.add_float_option("DeadtimeMonitor", "interval",
                          "minimum seconds between printed status lines "
                          "(messages arriving faster than this are still used "
                          "for the windowed average, just not printed)", 0.0)
    app.add_float_option("DeadtimeMonitor", "window",
                          "seconds of real-time over which to average the "
                          "instantaneous dead-time (beats the 1 s realTime "
                          "granularity that makes single-step instDT noisy)", 15.0)
    app.add_string_option("DeadtimeMonitor", "csv", "optional CSV log path", "")

    result = app.parse()
    if result is None:
        sys.exit(1)
    app.init_logger(result)

    topic = result.get_string("topic")
    interval = result.get_float("interval")
    window = result.get_float("window")
    csv_path = result.get_string("csv")

    connection = app.create_connection(result)
    if connection is None:
        print("Could not connect to messaging backend - is the server running?")
        sys.exit(1)

    writer = None
    fh = None
    if csv_path:
        import csv as _csv
        fh = open(csv_path, "w")
        writer = _csv.writer(fh)
        writer.writerow(["wall_s", "real_s", "live_s", "cum_dt_pct",
                         "inst_dt_pct", "count_rate_hz"])

    print("%-9s %8s %8s %9s %10s %12s"
          % ("wall_s", "real_s", "live_s", "cumDT%", "instDT%", "rate_Hz"))

    hist = deque()   # (real, live) samples, for windowed instantaneous DT
    state = {"t0": None, "last_print": 0.0}

    def on_run_info(message, source=None):
        try:
            payload = json.loads(message)
        except Exception:
            return
        info = pmsg.parse_run_info(payload)
        if not info.get("valid"):
            print("  (run-info invalid - is an acquisition running?)")
            return

        now = time.time()
        if state["t0"] is None:
            state["t0"] = now
        real = info["real_time_sec"]
        live = info["live_time_sec"]
        cum_dt = info["dead_time_percent"]
        rate = info["count_rate_hz"]

        # Windowed instantaneous dead-time: compare against the oldest sample
        # that is >= --window seconds of real-time back, so d_real is large and
        # the 1 s realTime granularity contributes negligible error. Falls back
        # to the immediately previous sample until the window fills.
        inst_dt = float("nan")
        base = None
        for s in hist:
            if real - s[0] >= window:
                base = s
            else:
                break
        if base is None and hist:
            base = hist[-1]
        if base is not None:
            d_real = real - base[0]
            d_live = live - base[1]
            if d_real > 0:
                inst_dt = (1.0 - d_live / d_real) * 100.0
        hist.append((real, live))
        # keep a little more than the window so an old-enough baseline is available
        while len(hist) > 2 and (real - hist[0][0]) > window + 60.0:
            hist.popleft()

        wall = now - state["t0"]
        if interval > 0 and (now - state["last_print"]) < interval:
            pass
        else:
            state["last_print"] = now
            istr = "%10.2f" % inst_dt if inst_dt == inst_dt else "%10s" % "-"
            print("%-9.1f %8.1f %8.2f %9.2f %s %12.1f"
                  % (wall, real, live, cum_dt, istr, rate))
        if writer:
            writer.writerow(["%.1f" % wall, "%.1f" % real, "%.3f" % live,
                             "%.3f" % cum_dt,
                             ("%.3f" % inst_dt) if inst_dt == inst_dt else "",
                             "%.1f" % rate])
            fh.flush()

    recv_cfg = prism.TextReceiverConfig()
    recv_cfg.source = topic
    receiver = app.create_text_receiver(result, connection, recv_cfg)
    receiver.on_receive(on_run_info)
    receiver.start()

    print("Listening on '%s' for run-info - press Ctrl+C to stop" % topic)

    try:
        while app.is_running():
            time.sleep(0.1)
    finally:
        receiver.stop()
        if fh:
            fh.close()
        connection.close()


if __name__ == "__main__":
    main()
