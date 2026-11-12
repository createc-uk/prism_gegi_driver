"""Offline CLI transport and installer checks; never contacts a detector."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('gegi_cli_test', str(ROOT / 'scripts/gegi'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class CliTests(unittest.TestCase):
    def fake_prism(self, reply, progress=False):
        receivers = {}
        def create_receiver(_options, _conn, config):
            receiver = types.SimpleNamespace(start=lambda: None, stop=lambda: None)
            receiver.on_receive = lambda cb: setattr(receiver, 'callback', cb)
            receivers[config.source] = receiver
            return receiver
        conn = types.SimpleNamespace(close=lambda: None)
        def send(text):
            request = json.loads(text)
            receiver = next(v for k, v in receivers.items() if k.endswith('command_result'))
            # An unrelated result must never be accepted as our acknowledgement.
            receiver.callback(json.dumps({'request_id': 'other', 'success': True}))
            if reply is not None:
                receiver.callback(json.dumps(dict(reply, request_id=request['request_id'])))
            if progress:
                receivers['gegi.driver.energy_deposit'].callback(json.dumps({'dataType': 'DoubleValue', 'value': 662.0}))
            else:
                monitor = receivers.get('gegi.detector.run_info')
                if monitor:
                    for _ in range(2):
                        monitor.callback(json.dumps({'valid': True, 'real_time_sec': 0.0}))
            # Decorated Prism sender returns None, not bool.
        app = types.SimpleNamespace(parse=lambda: object(), create_connection=lambda _: conn,
            create_text_receiver=create_receiver,
            create_text_sender=lambda *_: types.SimpleNamespace(send=send))
        return types.SimpleNamespace(Application=lambda *_: app,
            TextReceiverConfig=types.SimpleNamespace, TextSenderConfig=types.SimpleNamespace)

    def test_success_and_refusal(self):
        for success in (True, False):
            with patch.dict(sys.modules, prism=self.fake_prism({'success': success, 'message': 'test'})):
                self.assertEqual(cli.request('gegi.detector.command', {'command': 'test'}, .01), 0 if success else 1)

    def test_timeout_ignores_unrelated_reply(self):
        with patch.dict(sys.modules, prism=self.fake_prism(None)):
            with self.assertRaisesRegex(RuntimeError, 'Outcome unknown'):
                cli.request('gegi.detector.command', {'command': 'test'}, .01)

    def test_start_acknowledgement_with_zero_time_is_not_success(self):
        with patch.dict(sys.modules, prism=self.fake_prism({'success': True, 'message': 'started'})):
            with self.assertRaisesRegex(RuntimeError, 'acquisition was NOT verified'):
                cli.request('gegi.data_recorder.command', {'command': 'start_timed_recording'}, .01)

    def test_start_requires_detector_progress(self):
        with patch.dict(sys.modules, prism=self.fake_prism({'success': True, 'message': 'started'}, progress=True)):
            self.assertEqual(cli.request('gegi.data_recorder.command', {'command': 'start_timed_recording'}, .01), 0)

    def test_advancing_run_clock_verifies_progress(self):
        progress = cli.AcquisitionProgress()
        progress.receive(json.dumps({'valid': True, 'real_time_sec': 0.0}))
        self.assertFalse(progress.ready.is_set())
        progress.receive(json.dumps({'valid': True, 'real_time_sec': 30.0}))
        self.assertTrue(progress.ready.is_set())

    def test_reject_invalid_duration_without_sending(self):
        with patch.object(sys, 'argv', ['gegi']), patch.object(cli.subprocess, 'call') as call:
            with self.assertRaises(ValueError):
                cli.main(['start', '7'])
            call.assert_not_called()

    def test_install_uninstall_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            rc = prefix / 'bashrc'
            rc.write_text('# preserve me\n')
            cmd = [sys.executable, str(ROOT / 'scripts/install_gegi.py'), '--prefix', tmp, '--shell-rc', str(rc)]
            subprocess.run(cmd, check=True, capture_output=True)
            subprocess.run(cmd, check=True, capture_output=True)
            self.assertEqual(rc.read_text().count('# >>> GeGi CLI'), 1)
            result = subprocess.run([str(prefix / 'bin/gegi-start'), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            subprocess.run(cmd + ['--uninstall'], check=True, capture_output=True)
            self.assertEqual(list((prefix / 'bin').iterdir()), [])
            self.assertEqual(rc.read_text().strip(), '# preserve me')
            (prefix / 'bin/p').write_text('unrelated')
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertEqual((prefix / 'bin/p').read_text(), 'unrelated')


if __name__ == '__main__':
    unittest.main()
