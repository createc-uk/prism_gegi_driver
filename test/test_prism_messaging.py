#!/usr/bin/env python3
"""Unit tests for the protocol-agnostic Prism wire and command contracts."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))

import prism_messages as pmsg  # noqa: E402
from prism_command_channel import CommandClient, CommandServer  # noqa: E402


class _Receiver(object):
    def __init__(self):
        self.callback = None
        self.running = False

    def on_receive(self, callback):
        self.callback = callback

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def deliver(self, message, source="test.topic"):
        self.callback(message, source)


class _Sender(object):
    def __init__(self, on_send=None):
        self.messages = []
        self.on_send = on_send

    def send(self, message):
        self.messages.append(message)
        if self.on_send is not None:
            self.on_send(message)


class TestScalarSchemas(unittest.TestCase):
    def test_double_value_matches_prism_wire_shape(self):
        payload = pmsg.make_double_value(12.5)
        self.assertEqual(payload, {"dataType": "DoubleValue", "value": 12.5})
        self.assertEqual(pmsg.parse_double_value(payload), 12.5)
        self.assertIsNone(pmsg.parse_double_value({"dataType": "IntValue", "value": 12}))

    def test_int_value_matches_prism_wire_shape(self):
        payload = pmsg.make_int_value(3)
        self.assertEqual(payload, {"dataType": "IntValue", "value": 3})
        self.assertEqual(pmsg.parse_int_value(payload), 3)
        self.assertIsNone(pmsg.parse_int_value({"dataType": "DoubleValue", "value": 3.0}))


class TestCustomSchemas(unittest.TestCase):
    def test_compton_event_round_trip(self):
        payload = pmsg.make_compton_event(
            10.25, "detector", 7,
            100.0, (1.0, 2.0, 3.0),
            200.0, (4.0, 5.0, 6.0),
            0.5, 0.04)
        parsed = pmsg.parse_compton_event(payload)
        self.assertEqual(payload["dataType"], "ComptonEvent")
        self.assertEqual(parsed["seq"], 7)
        self.assertEqual(parsed["reading_location_1"], (1.0, 2.0, 3.0))
        self.assertEqual(parsed["reading_location_2"], (4.0, 5.0, 6.0))

    def test_spectrum_uses_snake_case_wire_fields(self):
        payload = pmsg.make_spectrum(
            stamp=1.0, frame_id="detector", seq=2, end_time=1.25,
            real_time_ms=250, dead_time_ms=5, total_count=3,
            spectrum=[0, 1, 2])
        self.assertEqual(payload["dataType"], "Spectrum")
        self.assertEqual(payload["real_time_ms"], 250)
        self.assertEqual(payload["dead_time_ms"], 5)
        self.assertEqual(payload["total_count"], 3)
        self.assertEqual(payload["spectrum"], [0, 1, 2])


class TestCommandChannel(unittest.TestCase):
    def test_server_dispatches_and_echoes_request_id(self):
        receiver = _Receiver()
        sender = _Sender()
        server = CommandServer(receiver, sender)
        server.on("clear", lambda params: (True, "cleared"))
        server.start()

        receiver.deliver(json.dumps({"command": "clear", "request_id": "r-1"}))
        result = json.loads(sender.messages[-1])

        self.assertEqual(result["dataType"], "CommandResult")
        self.assertEqual(result["command"], "clear")
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "cleared")
        self.assertEqual(result["request_id"], "r-1")
        server.stop()

    def test_client_correlates_result_over_plain_pubsub(self):
        result_receiver = _Receiver()

        def respond(command_text):
            command = json.loads(command_text)
            response = pmsg.make_command_result(
                command["command"], True, "pong",
                request_id=command["request_id"])
            result_receiver.deliver(json.dumps(response), "test.command_result")

        client = CommandClient(_Sender(on_send=respond), result_receiver)
        result = client.call({"command": "ping"}, timeout=0.5)

        self.assertIsNotNone(result)
        self.assertTrue(result["success"])
        self.assertEqual(result["command"], "ping")
        self.assertEqual(result["message"], "pong")
        self.assertTrue(result["request_id"])
        client.close()


if __name__ == "__main__":
    unittest.main()
