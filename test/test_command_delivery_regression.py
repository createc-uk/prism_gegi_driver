#!/usr/bin/env python3
"""No-hardware regression checks for Prism command delivery semantics."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', 'src', 'phds_gegi_driver')))
from prism_command_channel import CommandServer, CommandClient


class FakeReceiver:
    def __init__(self):
        self.callback = None

    def on_receive(self, callback):
        self.callback = callback

    def start(self):
        pass

    def stop(self):
        pass

    def deliver(self, message):
        self.callback(json.dumps(message) if not isinstance(message, str) else message,
                      "gegi.diagnostics.test")


class FakeSender:
    def __init__(self, on_send=None, response=None):
        self.messages = []
        self.on_send = on_send
        self.response = response

    def send(self, message):
        self.messages.append(message)
        if self.on_send is not None:
            self.on_send(message)
        return self.response


class TestCommandSafety(unittest.TestCase):
    def test_duplicate_id_does_not_repeat_side_effect(self):
        receiver, sender = FakeReceiver(), FakeSender()
        server = CommandServer(receiver, sender)
        calls = []
        server.on('start', lambda command: (calls.append(command) or True, 'started'))
        server.start()
        msg = {'dataType': 'Command', 'command': 'start', 'request_id': 'same-id'}
        receiver.deliver(msg)
        receiver.deliver(msg)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(sender.messages[0]), json.loads(sender.messages[1]))
        receiver.deliver({'command': 'stop', 'request_id': 'same-id'})
        self.assertEqual(len(calls), 1)
        self.assertFalse(json.loads(sender.messages[-1])['success'])
        self.assertIn('already used',json.loads(sender.messages[-1])['message'])
        server.stop()

    def test_plain_json_and_typed_json_both_supported(self):
        receiver, sender = FakeReceiver(), FakeSender()
        server = CommandServer(receiver, sender)
        server.on('ping', lambda _: (True, 'pong'))
        server.start()
        receiver.deliver({'command': 'ping', 'request_id': 'untyped'})
        receiver.deliver({'dataType': 'Command', 'command': 'ping', 'request_id': 'typed'})
        self.assertTrue(all(json.loads(r)['success'] for r in sender.messages))
        self.assertEqual([json.loads(r)['request_id'] for r in sender.messages],
                         ['untyped', 'typed'])
        server.stop()

    def test_malformed_command_is_rejected_without_handler_side_effect(self):
        receiver, sender = FakeReceiver(), FakeSender()
        server = CommandServer(receiver, sender)
        server.on('start', mock.Mock(return_value=(True, 'started')))
        server.start()
        for msg in ('[]', '"hello"', '{"command":123}', '{"command":"start","request_id":2}'):
            receiver.deliver(msg)
            result = json.loads(sender.messages[-1])
            self.assertEqual(result['dataType'], 'CommandResult')
            self.assertFalse(result['success'])
            self.assertIn('Malformed', result['message'])
        server._handlers['start'].assert_not_called()
        server.stop()

    def test_handler_exception_still_returns_failure(self):
        receiver, sender = FakeReceiver(), FakeSender()
        server = CommandServer(receiver, sender)
        server.on('fail', lambda _: 1/0)
        server.start()
        receiver.deliver({'command': 'fail', 'request_id': 'error-test'})
        result = json.loads(sender.messages[0])
        self.assertFalse(result['success'])
        self.assertEqual(result['request_id'], 'error-test')
        self.assertIn('handler failed',result['message'])
        server.stop()

    def test_client_send_exception_cleans_pending_without_retry(self):
        response_receiver = FakeReceiver()
        def fail(_):
            raise RuntimeError('connection closed')
        sender = FakeSender(on_send=fail)
        client = CommandClient(sender, response_receiver)
        with self.assertRaisesRegex(RuntimeError, 'connection closed'):
            client.call({'command': 'start'}, timeout=0.01)
        self.assertFalse(client._pending)
        self.assertEqual(len(sender.messages), 1)
        client.close()

    def test_client_send_false_cleans_pending(self):
        receiver = FakeReceiver()
        sender = FakeSender(response=False)
        client = CommandClient(sender, receiver)
        with self.assertRaisesRegex(RuntimeError, 'sender rejected'):
            client.call({'command':'start'}, timeout=0.01)
        self.assertFalse(client._pending)
        self.assertEqual(len(sender.messages), 1)
        client.close()

    def test_client_timeout_cleans_pending_without_retry(self):
        receiver = FakeReceiver()
        sender = FakeSender()
        client = CommandClient(sender, receiver)
        self.assertIsNone(client.call({'command':'start'}, timeout=0.005))
        self.assertFalse(client._pending)
        self.assertEqual(len(sender.messages), 1)
        client.close()

    def test_result_sender_exception_does_not_kill_dispatcher(self):
        receiver = FakeReceiver()
        calls=[]
        sender = FakeSender(on_send=lambda _: (_ for _ in ()).throw(RuntimeError('offline')))
        server = CommandServer(receiver, sender)
        server.on('noop', lambda command: (calls.append(command) or True, 'ok'))
        server.start()
        receiver.deliver({'command': 'noop', 'request_id': 'repeat-id'})
        receiver.deliver({'command': 'noop', 'request_id': 'repeat-id'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(sender.messages), 2)
        server.stop()


class TestRecorderTracePropagation(unittest.TestCase):
    """Use an uninitialised recorder with mocked clients; no hardware touched."""

    def setUp(self):
        import threading
        from data_recorder_node import DataRecorderNode
        self.node = DataRecorderNode.__new__(DataRecorderNode)
        self.node.lock = threading.Lock()
        self.node.recording = False
        self.node.output_dir = '/tmp/not-used-gegi-command-test'
        self.calls = []
        self.node._start_recording = mock.Mock()
        self.node._stop_recording = mock.Mock()
        self.node.timer = None
        class StubDetector:
            def call(inner, command, timeout=5.0):
                self.calls.append(dict(command))
                return {'success': True, 'message': 'synthetic ack'}
        self.node._detector_client = StubDetector()
        self.node._node_clear_clients = []

    def test_start_propagates_original_id_as_trace_id(self):
        result, _ = self.node._handle_start({
            'command':'start_timed_recording',
            'duration_minutes':5,
            'request_id':'external-123'
        })
        self.assertTrue(result)
        self.assertEqual(self.calls, [{
            'command':'start_timed_acquisition',
            'duration_minutes':5,
            'trace_id':'external-123'
        }])
        self.node._start_recording.assert_called_once_with(5)

    def test_stop_propagates_original_id_as_trace_id(self):
        self.node.recording = True
        success, _ = self.node._handle_stop({
            'command':'stop_recording','request_id':'external-456'
        })
        self.assertTrue(success)
        self.assertEqual(self.calls, [{
            'command':'stop_acquisition',
            'trace_id':'external-456'
        }])
        self.node._stop_recording.assert_called_once()

    def test_deep_clear_propagates_original_id(self):
        success, _ = self.node._handle_clear_all({
            'command':'clear_all','request_id':'external-789'
        })
        self.assertTrue(success)
        self.assertEqual(self.calls[0]['command'],'clear_data_and_windows')
        self.assertEqual(self.calls[0]['trace_id'],'external-789')


if __name__ == '__main__':
    unittest.main()
