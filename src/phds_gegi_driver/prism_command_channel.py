#!/usr/bin/env python3
"""
Prism-based replacement for ROS services (Python side).

Mirrors include/phds_gegi_driver/command_channel.hpp. Prism's protocol-
agnostic Application layer has no request/reply primitive by design (using
NATS's native request/reply would tie every node to one backend and break
--protocol portability), so every former ROS service here is a pair of
pub/sub topics:

    <ns>.command         (in)  -- JSON {"command": "...", ...params,
                                         "request_id"?: "..."}
    <ns>.command_result  (out) -- JSON CommandResult, echoing "request_id"
                                         if the caller supplied one.

CommandServer dispatches incoming commands by name to registered handlers
and publishes the result. CommandClient is an optional convenience for
callers that want a synchronous call() (correlates by request_id with a
threading.Event -- no backend RPC feature is used, so this works on every
protocol Prism supports).
"""

import json
import threading
import uuid
from collections import OrderedDict

from prism_messages import make_command_result, now_seconds


class CommandServer(object):
    """Wraps one "*.command" receiver + one "*.command_result" sender.

    `command_receiver` must be a raw TextReceiver (has .on_receive(cb) /
    .start() / .stop()); `result_sender` must support .send(str).
    """

    def __init__(self, command_receiver, result_sender):
        self._command_receiver = command_receiver
        self._result_sender = result_sender
        self._handlers = {}
        self._dispatch_lock = threading.RLock()
        self._completed = OrderedDict()
        self._completed_limit = 1024
        self._command_receiver.on_receive(self._on_message)

    def on(self, command_name, handler):
        """Registers `handler(params: dict) -> (success: bool, message: str)`."""
        self._handlers[command_name] = handler

    def start(self):
        self._command_receiver.start()

    def stop(self):
        self._command_receiver.stop()
        # Break the receiver -> bound-method -> server reference cycle. Prism's
        # nanobind receiver objects are not collected reliably while that cycle
        # remains, which otherwise produces leak diagnostics at interpreter exit.
        self._command_receiver.on_receive(lambda _message, _source=None: None)

    def _on_message(self, message, source=None):
        command_name = ""
        request_id = ""
        success = False
        response_message = ""
        try:
            params = json.loads(message)
            if not isinstance(params, dict):
                raise ValueError("command must be an object")
            command_name = params.get("command", "")
            request_id = params.get("request_id", "")
            if not isinstance(command_name, str) or not isinstance(request_id, str):
                raise ValueError("command and request_id must be strings")
        except Exception as exc:
            response_message = "Malformed command message: {}".format(exc)
            self._result_sender.send(json.dumps(make_command_result(
                command_name, False, response_message,
                request_id=request_id or None, stamp=now_seconds())))
            return

        # Keep side-effectful commands at-most-once *within this process* for a
        # given request_id. An in-flight duplicate is serialized, and a later
        # duplicate gets the identical cached reply. Does not confer exactly-once
        # execution across driver restarts or MQTT/NATS network partitions.
        with self._dispatch_lock:
            if request_id and request_id in self._completed:
                prev_command, prev_result = self._completed[request_id]
                self._completed.move_to_end(request_id)
                if prev_command != command_name:
                    reply = make_command_result(
                        command_name, False,
                        "request_id already used for another command",
                        request_id=request_id, stamp=now_seconds())
                    output = json.dumps(reply)
                else:
                    output = prev_result
            else:
                try:
                    handler = self._handlers.get(command_name)
                    if handler is None:
                        response_message = "Unknown command: {}".format(command_name)
                    else:
                        success, response_message = handler(params)
                except Exception as exc:
                    response_message = "Command handler failed: {}".format(exc)
                output = json.dumps(make_command_result(
                    command_name, success, response_message,
                    request_id=request_id or None, stamp=now_seconds()))
                if request_id:
                    self._completed[request_id] = (command_name, output)
                    while len(self._completed) > self._completed_limit:
                        self._completed.popitem(last=False)
            self._result_sender.send(output)


class CommandClient(object):
    """Optional synchronous caller: publish a command and wait for the
    matching command_result (matched by request_id). Not used by the
    driver's own command handling, but available to any node (e.g.
    data_recorder_node) that wants to confirm a command before proceeding,
    without depending on any protocol-specific RPC feature.
    """

    def __init__(self, command_sender, result_receiver):
        self._command_sender = command_sender
        self._result_receiver = result_receiver
        self._lock = threading.Lock()
        self._pending = {}
        self._result_receiver.on_receive(self._on_result)
        self._result_receiver.start()

    def close(self):
        self._result_receiver.stop()
        # Break the receiver -> bound-method -> client reference cycle; see the
        # matching CommandServer.stop() cleanup above.
        self._result_receiver.on_receive(lambda _message, _source=None: None)

    def publish(self, command):
        """Fire-and-forget."""
        self._command_sender.send(json.dumps(command))

    def call(self, command, timeout=5.0):
        """Publishes `command` with a generated request_id and blocks (up
        to `timeout` seconds) for the correlated command_result. Returns
        the result dict, or None on timeout."""
        request_id = uuid.uuid4().hex
        command = dict(command)
        command["request_id"] = request_id

        event = threading.Event()
        with self._lock:
            self._pending[request_id] = {"event": event, "result": None}

        try:
            self._command_sender.send(json.dumps(command))
            got = event.wait(timeout)
        finally:
            with self._lock:
                entry = self._pending.pop(request_id, None)
        if not got or entry is None:
            return None
        return entry["result"]

    def _on_result(self, message, source=None):
        try:
            result = json.loads(message)
        except Exception:
            return
        request_id = result.get("request_id", "")
        if not request_id:
            return
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is not None:
                entry["result"] = result
                entry["event"].set()
