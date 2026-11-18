"""Process-level regression tests for Python CI collection and failure gates."""
import importlib.util
import ast
import json
import os
import signal
import socket
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/test-python.py'


class PythonRunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), 'shared Python test runner is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def invoke(self, *args, env=None):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), *args],
                              env=env, text=True, capture_output=True, timeout=10)

    def test_empty_skip_failure_and_import_error_fail_closed(self):
        bodies = ['pass', 'import unittest\n@unittest.skip("missing runtime")\nclass T(unittest.TestCase):\n def test_it(self): pass',
                  'import unittest\nclass T(unittest.TestCase):\n def test_it(self): self.fail("injected")',
                  'raise RuntimeError("import failed")']
        for body in bodies:
            with self.subTest(body=body):
                fixture = self.root / 'test_fixture.py'
                fixture.write_text(body)
                result = self.invoke('unit', str(fixture))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_passing_test_executes(self):
        marker = self.root / 'executed'
        fixture = self.root / 'test_fixture.py'
        fixture.write_text('import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_it(self): Path(' + repr(str(marker)) + ').touch()\n')
        result = self.invoke('unit', str(fixture))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    def test_regression_collection_contains_missing_files_without_install_duplicate(self):
        result = self.invoke('regression', '--list')
        self.assertEqual(result.returncode, 0, result.stderr)
        collected = result.stdout.splitlines()
        expected = ['scripts/test_backup_restore.py', 'scripts/test_raw_retention.py',
                    'scripts/test_storage_capacity.py', 'scripts/test_performance_metrics.py',
                    'scripts/test_operations_docs.py', 'scripts/validation/test_network_fixture.py',
                    'scripts/validation/test_rotation_probe.py', 'scripts/test_python_runner.py']
        self.assertTrue(set(expected) <= set(collected), collected)
        self.assertEqual(len(collected), len(set(collected)))
        self.assertNotIn('scripts/test_install.py', collected)
        self.assertNotIn('sensors/web/test_reactor_evidence.py', collected)

    def fake_docker(self, mode):
        docker = self.root / 'docker'
        docker.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time
from pathlib import Path
with Path(os.environ['DOCKER_TEST_LOG']).open('a') as stream:
 stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['image', 'inspect']:
 print('sha256:' + 'a'*64)
elif sys.argv[1] == 'create':
 print(sys.argv[sys.argv.index('--name')+1])
elif sys.argv[1] == 'start':
 if os.environ['DOCKER_TEST_MODE'] == 'failure': sys.exit(17)
 if os.environ['DOCKER_TEST_MODE'] == 'timeout': time.sleep(30)
''')
        docker.chmod(0o755)
        env = os.environ.copy()
        env.update(PATH=str(self.root) + os.pathsep + env['PATH'],
                   DOCKER_TEST_LOG=str(self.root / 'docker.jsonl'), DOCKER_TEST_MODE=mode)
        return env

    def test_image_failure_and_timeout_propagate_and_remove_owned_container(self):
        for mode, code in [('failure', 17), ('timeout', 124)]:
            with self.subTest(mode=mode):
                env = self.fake_docker(mode)
                result = self.invoke('image', '--image', 'test:local', '--timeout', '0.2', env=env)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                calls = [json.loads(s) for s in (self.root / 'docker.jsonl').read_text().splitlines()]
                created = [c[c.index('--name')+1] for c in calls if c[0] == 'create']
                self.assertTrue(created)
                self.assertEqual(calls[-1], ['rm', '-f', created[-1]])
                (self.root / 'docker.jsonl').unlink()

    def test_image_runs_both_suites_with_pinned_id_and_isolation(self):
        result = self.invoke('image', '--image', 'test:local', env=self.fake_docker('pass'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(s) for s in (self.root / 'docker.jsonl').read_text().splitlines()]
        creates = [c for c in calls if c[0] == 'create']
        self.assertEqual(len(creates), 2)
        for c in creates:
            self.assertIn('sha256:' + 'a'*64, c)
            self.assertEqual(c[c.index('--network')+1], 'none')
            self.assertIn('--read-only', c)
            self.assertIn('CHEESETRAP_REQUIRE_TWISTED_TESTS=1', c)
            self.assertEqual(c[c.index('--entrypoint')+1], '/opencanary/.venv/bin/python')
            self.assertTrue(all('readonly' in c[i+1] for i, x in enumerate(c) if x == '--mount'))
        self.assertIn('unit', creates[0])
        self.assertEqual(creates[1][-1], '/checks/test_reactor_evidence.py')
        self.assertEqual(len([c for c in calls if c[0] == 'rm']), 2)

    def test_image_interruption_removes_owned_container(self):
        env = self.fake_docker('timeout')
        process = subprocess.Popen([sys.executable, '-B', str(SCRIPT), 'image', '--image', 'test:local'],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            log = self.root / 'docker.jsonl'
            deadline = time.monotonic() + 5
            calls = []
            while time.monotonic() < deadline and process.poll() is None:
                calls = [json.loads(s) for s in log.read_text().splitlines()] if log.exists() else []
                if any(c[0] == 'start' for c in calls):
                    break
                time.sleep(0.02)
            self.assertTrue(any(c[0] == 'start' for c in calls), 'container never started')
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=6)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            calls = [json.loads(s) for s in log.read_text().splitlines()]
            name = next(c[c.index('--name')+1] for c in calls if c[0] == 'create')
            self.assertEqual(calls[-1], ['rm', '-f', name])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def isolated_regressions(self):
        # Replace only the fixed inventory; execute the real CLI and gate code.
        local = self.root / 'scripts/test-python.py'
        local.parent.mkdir(exist_ok=True)
        tree = ast.parse(SCRIPT.read_text())
        inventory = next(n for n in tree.body if isinstance(n, ast.Assign) and
                         any(isinstance(t, ast.Name) and t.id == 'REGRESSIONS' for t in n.targets))
        inventory.value = ast.Tuple(elts=[], ctx=ast.Load())
        local.write_text(ast.unparse(ast.fix_missing_locations(tree)) + '\n')
        return local

    def coordinated_regressions(self, address, *, fail=False, descendants=False):
        local = self.isolated_regressions()
        (self.root / 'state').write_text(json.dumps(dict(active=0, maximum=0, started=[], finished=[])))
        for index in range(4):
            fixture = local.with_name(f'test_{index:02}_runner.py')
            fixture.write_text('''import fcntl,json,os,pathlib,socket,subprocess,sys,unittest
root=pathlib.Path(__file__).resolve().parents[1]
def update(start):
 with (root/'state').open('r+') as stream:
  fcntl.flock(stream,fcntl.LOCK_EX)
  state=json.load(stream);state['active']+=1 if start else -1
  state['maximum']=max(state['maximum'],state['active'])
  state['started' if start else 'finished'].append(INDEX)
  stream.seek(0);json.dump(state,stream);stream.truncate()
class T(unittest.TestCase):
 def test_it(self):
  child=None
  if DESCENDANTS:
   code="import pathlib,signal,sys\\ndef done(*args):\\n pathlib.Path(sys.argv[1]).touch();sys.exit(0)\\nsignal.signal(signal.SIGTERM,done)\\nprint('ready',flush=True)\\nsignal.pause()"
   child=subprocess.Popen([sys.executable,'-c',code,str(root/('term-'+str(INDEX)))],stdout=subprocess.PIPE,text=True)
   self.assertEqual(child.stdout.readline().strip(),'ready')
  update(True)
  try:
   with socket.create_connection(ADDRESS,timeout=5) as connection:
    connection.sendall((json.dumps(dict(index=INDEX,pid=os.getpid(),child=child.pid if child else None))+'\\n').encode())
    self.assertEqual(connection.recv(1),b'x')
   if FAIL and INDEX in (0,2):self.fail('synthetic suite failure')
  finally:
   if child:
    child.wait(timeout=3);child.stdout.close()
   update(False)
'''.replace('INDEX', str(index)).replace('ADDRESS', repr(address))
                .replace('DESCENDANTS', repr(descendants)).replace('FAIL', repr(fail)))
        return local

    def test_regression_suites_overlap_bounded_once_and_keep_other_results_after_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed), socket.socket() as server:
                server.bind(('127.0.0.1', 0)); server.listen(4); server.settimeout(2)
                local = self.coordinated_regressions(server.getsockname(), fail=failed)
                process = subprocess.Popen([sys.executable, '-B', str(local), 'regression', '--timeout', '5'],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                connections = []
                try:
                    for _ in range(2):
                        try: connection, _ = server.accept()
                        except socket.timeout: self.fail('two independent regression suites must start together')
                        connection.recv(1024); connections.append(connection)
                    server.settimeout(.1)
                    with self.assertRaises(socket.timeout): server.accept()
                    connections[0].sendall(b'x'); connections[0].close()
                    server.settimeout(2)
                    connection, _ = server.accept(); connection.recv(1024); connections.append(connection)
                    for connection in connections[1:]: connection.sendall(b'x'); connection.close()
                    connection, _ = server.accept(); connection.recv(1024); connections.append(connection)
                    connection.sendall(b'x'); connection.close()
                    stdout, stderr = process.communicate(timeout=6)
                    self.assertEqual(process.returncode, 1 if failed else 0, stdout+stderr)
                    state = json.loads((self.root / 'state').read_text())
                    self.assertEqual(state['maximum'], 2)
                    self.assertEqual(state['active'], 0)
                    self.assertEqual(sorted(state['started']), list(range(4)))
                    self.assertEqual(sorted(state['finished']), list(range(4)))
                    self.assertEqual('PASS_PYTHON_REGRESSION' in stdout, not failed)
                    if failed:
                        for index in (0, 2):
                            self.assertIn(f'FAIL: Python regression exited 1: scripts/test_{index:02}_runner.py', stderr)
                finally:
                    for connection in connections: connection.close()
                    if process.poll() is None:
                        process.send_signal(signal.SIGTERM); process.communicate(timeout=6)

    def test_regression_timeout_does_not_hide_or_omit_other_files(self):
        local = self.isolated_regressions()
        local.with_name('test_00_runner.py').write_text('import threading\nthreading.Event().wait()\n')
        for index in range(1, 4):
            local.with_name(f'test_{index:02}_runner.py').write_text('import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_it(self): Path('+repr(str(self.root / str(index)))+').touch()\n')
        result = subprocess.run([sys.executable, '-B', str(local), 'regression', '--timeout', '.3'],
                                capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 124, result.stdout+result.stderr)
        self.assertTrue(all((self.root / str(index)).exists() for index in range(1, 4)))
        self.assertNotIn('PASS_PYTHON_REGRESSION', result.stdout)

    def test_regression_cancellation_stops_both_groups_and_descendants_without_launching_queued_files(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(4); server.settimeout(2)
            local = self.coordinated_regressions(server.getsockname(), descendants=True)
            process = subprocess.Popen([sys.executable, '-B', str(local), 'regression', '--timeout', '5'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            connections, children = [], []
            try:
                for _ in range(2):
                    try: connection, _ = server.accept()
                    except socket.timeout: self.fail('cancellation fixture needs two live owned suite groups')
                    connections.append(connection)
                    data = json.loads(connection.recv(1024))
                    self.assertEqual(os.getpgid(data['child']), data['pid'])
                    children.append(data)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 143, stdout+stderr)
                state = json.loads((self.root / 'state').read_text())
                self.assertEqual(sorted(state['started']), [0, 1])
                self.assertEqual(state['active'], 0)
                for data in children:
                    self.assertTrue((self.root / ('term-'+str(data['index']))).is_file())
                    for pid in (data['pid'], data['child']):
                        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                self.assertNotIn('PASS_PYTHON_REGRESSION', stdout)
            finally:
                for connection in connections: connection.close()
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM); process.communicate(timeout=6)
                for data in children:
                    try: os.killpg(data['pid'], signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_failed_python_command_cannot_produce_pass_automated(self):
        fixture = self.root / 'test_fixture.py'
        fixture.write_text('import unittest\nclass T(unittest.TestCase):\n def test_it(self): self.fail("injected")\n')
        spec = importlib.util.spec_from_file_location('acceptance', ROOT / 'scripts/test-acceptance.py')
        acceptance = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acceptance)
        code = acceptance.run_command([sys.executable, '-B', str(SCRIPT), 'unit', str(fixture)],
                                      cwd=ROOT, env=os.environ.copy(), log=self.root / 'acceptance.log', timeout=5)
        self.assertNotEqual(code, 0)
        status = acceptance.run_status([{'status': 'BLOCKED'}],
                                       [{'name': 'runner-tests', 'status': 'PASS' if code == 0 else 'FAIL'}], True)
        self.assertEqual(status, 'FAIL')


if __name__ == '__main__':
    unittest.main()
