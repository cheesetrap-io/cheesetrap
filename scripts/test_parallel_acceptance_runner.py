"""Owned processes prove independent acceptance checks overlap and fail closed."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

SPEC = importlib.util.spec_from_file_location('acceptance', Path(__file__).with_name('test-acceptance.py'))
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class IndependentChecksTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(RUNNER, 'run_independent', None)),
                        'independent acceptance checks need an owned concurrent runner')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = self.root / 'check.py'
        self.fixture.write_text('''import json, os, pathlib, signal, socket, subprocess, sys
root, name = pathlib.Path(sys.argv[1]), sys.argv[2]
def record(action):
 with (root/'events').open('a') as stream:
  stream.write(json.dumps([action,name,sys.argv[3:]])+'\\n')
record('start')
with socket.socket(socket.AF_UNIX) as connection:
 connection.settimeout(3)
 connection.connect(str(root/'gate'))
 if os.environ.get('BLOCK'):
  child=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'],start_new_session=True)
  (root/(name+'.child')).write_text(str(child.pid))
  def stop(signum,frame): raise SystemExit(128+signum)
  signal.signal(signal.SIGTERM,stop)
  connection.sendall(name.encode())
  try: signal.pause()
  finally:
   child.terminate(); child.wait(timeout=2)
   (root/(name+'.clean')).touch()
 else:
  connection.sendall(name.encode())
  if connection.recv(10)!=b'run': sys.exit(18)
record('end')
print('private output '+name)
if name==os.environ.get('FAIL'): sys.exit(9)
''')
        self.groups = [[self.check('integration')], [self.check(name) for name in ('runner-tests', 'go', 'vet')]]
        self.env = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}

    def check(self, name):
        return {'name': name, 'command': [sys.executable, '-B', str(self.fixture), str(self.root), name, '--keep'],
                'cwd': str(self.root), 'timeout': 5, 'log': str(self.root / (name+'.log')),
                'status': 'FAIL', 'returncode': None, 'process_status': 'not_started'}

    def exercise(self, fail=''):
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.root / 'gate'))
            server.listen()
            server.settimeout(5)
            errors = []
            def release():
                connections = []
                try:
                    # Neither first check can finish before its sibling starts.
                    # This proves overlap without a performance threshold.
                    for _ in range(2):
                        connection, _ = server.accept()
                        connections.append(connection)
                        connection.recv(100)
                    for connection in connections: connection.sendall(b'run')
                    for _ in range(2):
                        connection, _ = server.accept()
                        connections.append(connection)
                        connection.recv(100)
                        connection.sendall(b'run')
                except (OSError, TimeoutError) as error:
                    errors.append(type(error).__name__)
                finally:
                    for connection in connections: connection.close()
            thread = threading.Thread(target=release)
            thread.start()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    RUNNER.run_independent(self.groups, env=dict(self.env, FAIL=fail))
            finally:
                thread.join(timeout=6)
        self.assertEqual(errors, [])
        rows = [row for group in self.groups for row in group]
        active, peak, started = set(), 0, []
        for action, name, flags in map(json.loads, (self.root/'events').read_text().splitlines()):
            self.assertEqual(flags, ['--keep'])
            if action == 'start':
                started.append(name)
                active.add(name)
                peak = max(peak, len(active))
            else: active.remove(name)
        self.assertEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertCountEqual(started, ['integration', 'runner-tests', 'go', 'vet'])
        for row in rows:
            self.assertIn('private output '+row['name'], Path(row['log']).read_text())
        return rows

    def test_independent_checks_overlap_with_two_processes_and_no_lost_results(self):
        rows = self.exercise()
        self.assertTrue(all(row['status'] == 'PASS' and row['returncode'] == 0 for row in rows))

    def test_failure_remains_red_and_does_not_suppress_other_checks(self):
        rows = self.exercise(fail='runner-tests')
        failed = next(row for row in rows if row['name'] == 'runner-tests')
        self.assertEqual((failed['status'], failed['returncode']), ('FAIL', 9))
        self.assertEqual(RUNNER.run_status([], rows, automated=True), 'FAIL')

    def test_empty_groups_cannot_succeed(self):
        for groups in ([], [[]], [self.groups[0], []]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                RUNNER.run_independent(groups, env=self.env)

    def test_start_failure_stays_red_and_the_other_lane_finishes(self):
        failed = self.check('integration')
        failed['command'] = [str(self.root/'does-not-exist')]
        passed = self.check('go')
        passed['command'] = [sys.executable, '-c', 'print("completed")']
        with contextlib.redirect_stdout(io.StringIO()):
            RUNNER.run_independent([[failed], [passed]], env=self.env)
        self.assertEqual((failed['status'], failed['returncode'], failed['process_status']),
                         ('FAIL', None, 'start_failed'))
        self.assertEqual(passed['status'], 'PASS')

    def test_uncooperative_process_is_killed_after_cleanup_grace(self):
        pidfile = self.root / 'uncooperative.pid'
        blocked, passed = self.check('integration'), self.check('go')
        blocked.update(timeout=1, command=[sys.executable, '-c',
            f'import os,pathlib,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); signal.pause()'])
        passed['command'] = [sys.executable, '-c', 'print("completed")']
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RUNNER.AcceptanceTimeout):
                RUNNER.run_independent([[blocked], [passed]], env=self.env, cleanup_timeout=0.2)
            self.assertEqual(blocked['process_status'], 'timed_out')
            self.assertEqual(RUNNER.run_status([], [blocked, passed], automated=True), 'FAIL')
            with self.assertRaises(ProcessLookupError): os.kill(int(pidfile.read_text()), 0)
        finally:
            if pidfile.exists():
                try: os.killpg(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass

    def test_cancellation_waits_for_both_owned_cleanups_and_stops_queued_checks(self):
        self.stop_groups()

    def test_timeout_waits_for_both_owned_cleanups_and_stops_queued_checks(self):
        self.groups[0][0]['timeout'] = 1
        self.stop_groups(timeout=True)

    def stop_groups(self, timeout=False):
        config = self.root / 'groups.json'
        config.write_text(json.dumps(self.groups))
        code = '''import importlib.util, json, os, sys
spec=importlib.util.spec_from_file_location('runner',sys.argv[1])
r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
r.install_signal_handlers()
try: r.run_independent(json.load(open(sys.argv[2])),env=dict(os.environ))
except r.AcceptanceInterrupted: sys.exit(143)
except r.AcceptanceTimeout: sys.exit(124)
'''
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.root/'gate'))
            server.listen()
            server.settimeout(5)
            process = subprocess.Popen([sys.executable, '-B', '-c', code, RUNNER.__file__, str(config)],
                                       env=dict(self.env, BLOCK='1'), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, start_new_session=True)
            connections = []
            try:
                for _ in range(2):
                    connection, _ = server.accept()
                    connections.append(connection)
                    connection.recv(100)
                if not timeout: process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 124 if timeout else 143, output)
                for name in ('integration', 'runner-tests'):
                    self.assertTrue((self.root/(name+'.clean')).exists(), 'owned cleanup must finish')
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((self.root/(name+'.child')).read_text()), 0)
                self.assertFalse((self.root/'go.log').exists(), 'queued work must not start on cancel')
            finally:
                for connection in connections: connection.close()
                for child in self.root.glob('*.child'):
                    try: os.killpg(int(child.read_text()), signal.SIGKILL)
                    except ProcessLookupError: pass
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
