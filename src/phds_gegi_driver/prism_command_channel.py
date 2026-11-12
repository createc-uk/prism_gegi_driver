#!/usr/bin/env python3
"""Protocol-agnostic Prism command/reply channels.

Commands are JSON text (optionally with dataType='Command'), published to
<namespace>.command. Results are JSON CommandResult text on the matching
<namespace>.command_result topic. The request_id correlates a reply with the
caller; do not infer hardware state from a successful publish or TCP write.

CommandServer provides a bounded, in-process duplicate-request cache so a
caller can inspect/re-subscribe safely. This is NOT exactly-once execution
across restarts or network partitions: never blindly retry detector actions
after an ambiguous timeout.
"""

from collections import OrderedDict
import json
import logging
import threading
import uuid

from prism_messages import make_command_result, now_seconds


logger = logging.getLogger(__name__)


class CommandServer(object):
    """Dispatch one raw TextReceiver to registered side-effectful handlers."""

    def __init__(self, command_receiver, result_sender):
        self._command_receiver = command_receiver
        self._result_sender = result_sender
        self._handlers = {}
        self._dispatch_lock = threading.RLock()
        self._completed = OrderedDict()
        self._completed_limit = 1024
        self._command_receiver.on_receive(self._on_message)

    def on(self, command_name, handler):
        """Register handler(dict) -> (success: bool, message: str)."""
        self._handlers[command_name] = handler

    def start(self):
        self._command_receiver.start()

    def stop(self):
        self._command_receiver.stop()
        # Break receiver -> bound method -> CommandServer reference cycle.
        self._command_receiver.on_receive(lambda _message, _source=None: None)

    def _on_message(self, message, source=None):
        command_name = ""
        request_id = ""
        try:
            params = json.loads(message)
            if not isinstance(params, dict):
                raise ValueError("command must be a JSON object")
            command_name = params.get("command", "")
            request_id = params.get("request_id", "")
            if not isinstance(command_name, str) or not command_name:
                raise ValueError("command must be a non-empty string")
            if not isinstance(request_id, str):
                raise ValueError("request_id must be a string")
        except Exception as exc:
            logger.warning("Rejected malformed Prism command on %s: %s", source, exc)
            reply = make_command_result(
                command_name if isinstance(command_name, str) else "",
                False, "Malformed command message: {}".format(exc),
                request_id=request_id if isinstance(request_id, str) else None,
                stamp=now_seconds())
            self._send_result(json.dumps(reply), command_name, request_id)
            return

        logger.info("Prism command received: topic=%s command=%s request_id=%s",
                    source, command_name, request_id or "<none>")
        # Serialize duplicate IDs and side-effectful dispatch within this
        # process. A completed request returns exactly the cached reply.
        with self._dispatch_lock:
            if request_id and request_id in self._completed:
                previous_command, previous_reply = self._completed[request_id]
                self._completed.move_to_end(request_id)
                if previous_command == command_name:
                    logger.info("Replaying completed Prism reply: command=%s request_id=%s",
                                command_name, request_id)
                    output = previous_reply
                else:
                    output = json.dumps(make_command_result(
                        command_name, False,
                        "request_id already used for another command",
                        request_id=request_id, stamp=now_seconds()))
            else:
                try:
                    handler = self._handlers.get(command_name)
                    if handler is None:
                        success, description = False, "Unknown command: {}".format(command_name)
                    else:
                        success, description = handler(params)
                except Exception:
                    logger.exception("Command handler crashed: command=%s request_id=%s",
                                     command_name, request_id)
                    success, description = False, "Command handler failed; inspect driver logs"
                output = json.dumps(make_command_result(
                    command_name, success, description,
                    request_id=request_id or None, stamp=now_seconds()))
                if request_id:
                    self._completed[request_id] = (command_name, output)
                    while len(self._completed) > self._completed_limit:
                        self._completed.popitem(last=False)

            self._send_result(output, command_name, request_id)

    def _send_result(self, payload, command_name, request_id):
        try:
            status = self._result_sender.send(payload)
            if status is False:
                logger.error("Prism command reply publish rejected: command=%s request_id=%s",
                             command_name, request_id)
            else:
                logger.info("Prism command reply published: command=%s request_id=%s success=%s",
                            command_name, request_id, json.loads(payload).get("success"))
        except Exception:
            # Keep the receiver alive; do NOT repeat the side-effectful command.
            logger.exception("Prism command reply publish raised: command=%s request_id=%s",
                             command_name, request_id)


class CommandClient(object):
    """Correlate ordinary pub/sub command and result messages by request_id."""

    def __init__(self, command_sender, result_receiver):
        self._command_sender = command_sender
        self._result_receiver = result_receiver
        self._lock = threading.Lock()
        self._pending = {}
        self._result_receiver.on_receive(self._on_result)
        self._result_receiver.start()

    def close(self):
        self._result_receiver.stop()
        self._result_receiver.on_receive(lambda _message, _source=None: None)

    def publish(self, command):
        """Fire-and-forget, intended for non-critical notifications only."""
        status = self._command_sender.send(json.dumps(command))
        if status is False:
            raise RuntimeError("Prism sender rejected command")
        return status

    def call(self, command, timeout=5.0):
        """Send once, wait for a correlated reply; NEVER retry on timeout.

        A timeout means the outcome is unknown; detector actions may already
        have executed, even when no reply reached this process.
        """
        request_id = uuid.uuid4().hex
        command = dict(command)
        command["request_id"] = request_id
        event = threading.Event()
        with self._lock:
            self._pending[request_id] = {"event": event, "result": None}
        logger.info("Prism command forwarding: command=%s request_id=%s trace_id=%s",
                    command.get("command", ""), request_id, command.get("trace_id", ""))
        try:
            status = self._command_sender.send(json.dumps(command))
            # Decorated Prism Python senders return None on success.
            if status is False:
                raise RuntimeError("Prism sender rejected command")
            if not event.wait(timeout):
                logger.warning("Prism command reply timed out: command=%s request_id=%s timeout=%.3fs",
                               command.get("command", ""), request_id, timeout)
                return None
            with self._lock:
                result = self._pending[request_id]["result"]
            logger.info("Prism command reply received: command=%s request_id=%s success=%s",
                        command.get("command", ""), request_id, result.get("success"))
            return result
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _on_result(self, message, source=None):
        try:
            result = json.loads(message)
            if not isinstance(result, dict):
                return
            request_id = result.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                return
        except (TypeError, ValueError):
            return
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is not None:
                entry["result"] = result
                entry["event"].set()
