#!/usr/bin/env python3
"""Safe Prism/NATS command endpoint for isolated integration tests.

This handler implements ONLY the synthetic diagnostic_ping command on
'gegi.diagnostics.command'. It never imports the detector/TCP driver and
never sends hardware control commands. Start with
  PRISM_SERVER=127.0.0.1 PRISM_PORT=14222 python3 test/mock_prism_command_endpoint.py
"""
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))

import prism
from prism_command_channel import CommandServer


TOPIC = 'gegi.diagnostics.command'


def main():
    args = ['prism_command_probe', '--protocol', 'nats',
            '--server', os.getenv('PRISM_SERVER', '127.0.0.1'),
            '--port', os.getenv('PRISM_PORT', '14222')]
    app = prism.Application('prism_command_probe', 'Safe synthetic endpoint', args)
    parsed = app.parse()
    conn = app.create_connection(parsed)
    if conn is None:
        raise RuntimeError('Cannot connect to isolated NATS server')
    receiver_cfg = prism.TextReceiverConfig()
    receiver_cfg.source = TOPIC
    receiver = app.create_text_receiver(parsed, conn, receiver_cfg)
    sender_cfg = prism.TextSenderConfig()
    sender_cfg.destination = TOPIC + '_result'
    sender = app.create_text_sender(parsed, conn, sender_cfg)
    server = CommandServer(receiver, sender)
    counter = {'count': 0}
    def ping(payload):
        counter['count'] += 1
        return True, 'diagnostic ack: {}'.format(counter['count'])
    server.on('diagnostic_ping', ping)
    server.start()
    print('READY: '+TOPIC, flush=True)
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    try:
        while not done.wait(0.2):
            pass
    finally:
        server.stop()
        conn.close()
    print('STOPPED: count={}'.format(counter['count']),flush=True)


if __name__ == '__main__':
    main()
