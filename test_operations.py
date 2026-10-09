"""Synthetic subprocesses and metadata only; never connect to any service."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import service
import bot


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        service.initialize(self.state, bot.KNOWN_PRIOR_RESERVATION_USD)

    def test_rotation_redaction_and_numeric_metadata(self):
        log = service.OperationalLog(self.state, max_bytes=512)
        for i in range(30):
            log.emit('heartbeat', pid=i, secret='SYNTHETIC_SECRET', reason='SYNTHETIC_SECRET', url='SYNTHETIC_SECRET')
        log.close()
        files = list(self.state.glob('metadata.log*'))
        self.assertLessEqual(len(files), 4)
        self.assertGreater(len(files), 1)
        for path in files:
            text = path.read_text()
            self.assertNotIn('SYNTHETIC_SECRET', text)
            self.assertLessEqual(path.stat().st_size, 512)
            for line in text.splitlines():
                row = json.loads(line)
                self.assertIsInstance(row['boot_id'], int)
                self.assertIsInstance(row['monotonic_ms'], int)
        self.assertIsNone(service.safe_event('{"status":"secret exception"}'))

    def test_unclean_marker_survives_abrupt_process_and_new_boot(self):
        script = "import service,os; from pathlib import Path; l=service.OperationalLog(Path(__import__('sys').argv[1])); l.begin(); os._exit(9)"
        proc = subprocess.run([sys.executable, '-B', '-c', script, str(self.state)],
            cwd=Path(service.__file__).parent, env={'PATH': '/usr/bin:/bin'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 9)
        self.assertFalse(json.loads((self.state / 'lifecycle.json').read_text())['clean_exit'])
        log = service.OperationalLog(self.state)
        log.begin()
        log.finish()
        log.close()
        events = [json.loads(l)['status'] for l in (self.state / 'metadata.log').read_text().splitlines()]
        self.assertIn('prior_unclean_exit', events)
        self.assertTrue(json.loads((self.state / 'lifecycle.json').read_text())['clean_exit'])

    def test_stale_ready_reports_unknown_not_proven_kill(self):
        service.atomic_json(self.state / 'status.json', {'state': 'ready', 'ready': True,
            'supervisor_pid': 99999999, 'supervisor_start': 1, 'worker_pid': 99999998, 'worker_start': 1,
            'updated_utc_epoch': int(time.time()) - 1000})
        result = service.status(self.state)
        self.assertFalse(result['ready'])
        self.assertEqual(result['state'], 'unclean_stop')
        self.assertEqual(result['diagnosis'], 'unknown_abrupt_stop_environment_loss_possible')

    def run_supervisor(self, exit_code=None):
        worker = ("import time; print('{\"status\":\"ready\"}',flush=True); time.sleep(30)" if exit_code is None
                  else f"import sys; print('{{\"status\":\"operation_failed\"}}',flush=True); sys.exit({exit_code})")
        script = """import service,sys,subprocess
from pathlib import Path
state=Path(sys.argv[1]); real=subprocess.Popen
service.deployment=lambda *a,**k:{'root':str(Path.cwd()),'commit':'a'*40}
def child(*a,**k):
    return real([sys.executable,'-B','-c',sys.argv[2]],**k)
service.subprocess.Popen=child
lock=service.singleton(state)
service.supervise(state,lock.fileno())
"""
        return subprocess.Popen([sys.executable, '-B', '-c', script, str(self.state), worker],
            cwd=Path(service.__file__).parent, env={'PATH': '/usr/bin:/bin'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_unexpected_worker_exit_recorded(self):
        proc = self.run_supervisor(7)
        self.assertEqual(proc.wait(5), 0)
        result = service.status(self.state)
        self.assertEqual(result['worker_exit_code'], 7)
        self.assertEqual(result['state'], 'halted')
        self.assertTrue(result['clean_exit'])
        events = [json.loads(x) for x in (self.state / 'metadata.log').read_text().splitlines()]
        self.assertTrue(any(x['status'] == 'worker_exit' and x['exit_code'] == 7 for x in events))

    def test_signal_shutdown_and_heartbeat(self):
        proc = self.run_supervisor()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (self.state / 'status.json').exists() and service.status(self.state).get('ready'):
                    break
                time.sleep(.02)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(5), 0)
            result = service.status(self.state)
            self.assertEqual(result['shutdown_signal'], 15)
            self.assertTrue(result['clean_exit'])
            events = [json.loads(x)['status'] for x in (self.state / 'metadata.log').read_text().splitlines()]
            self.assertIn('heartbeat', events)
            self.assertIn('signal_received', events)
            self.assertIn('shutdown_complete', events)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    unittest.main()
