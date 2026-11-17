"""Regression coverage for integration discovery and Go JSON result gating."""
import importlib.util
import json
import os
import runpy
import signal
import sys
import time
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import contextlib
from collections import Counter
import io
import re
import socket
import threading

SPEC = importlib.util.spec_from_file_location("integration_runner", Path(__file__).with_name("test-integration.py"))
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def output(*events):
    return "\n".join(json.dumps(event) for event in events)


class BatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/test-integration.py').write_text('''import json, os, pathlib, signal, socket, sys
root = pathlib.Path(__file__).resolve().parents[1]
name = sys.argv[1]
def record(action):
 with (root/'events').open('a') as stream:
  stream.write(json.dumps([action, name, sys.argv[2:]])+'\\n')
record('start')
if os.environ.get('PROBE_BLOCK'):
 def stop(signum, frame): raise SystemExit(128+signum)
 signal.signal(signal.SIGTERM, stop)
 signal.signal(signal.SIGINT, stop)
 (root/('ready-'+name.removeprefix('./'))).write_text(str(os.getpid()))
 try:
  with socket.socket(socket.AF_UNIX) as connection:
   connection.connect(str(root/'gate'))
   connection.sendall(name.encode())
   signal.pause()
 finally: (root/('clean-'+name.removeprefix('./'))).touch()
else:
 with socket.socket(socket.AF_UNIX) as connection:
  connection.settimeout(1)
  connection.connect(str(root/'gate'))
  connection.sendall(name.encode())
  try: released = connection.recv(10)
  except TimeoutError: sys.exit(17)
  if released != b'run': sys.exit(18)
 record('end')
 print('completed '+name, flush=True)
 if name == os.environ.get('PROBE_FAIL'): sys.exit(9)
''')
        self.batches = [([f'./{name}'], ['-run', '^TestFixture$']) for name in ('one', 'two', 'three', 'four')]
        self.env = {k: os.environ[k] for k in ('PATH', 'HOME') if k in os.environ}

    def exercise(self, failure=None):
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(self.root / 'gate'))
        server.listen()
        server.settimeout(5)
        errors = []
        def release():
            connections = []
            try:
                # Three workers must start before any can finish. A smaller
                # concurrency cap fails the verdict, not a timing comparison.
                for _ in range(3):
                    connection, _ = server.accept()
                    connections.append(connection)
                    connection.recv(100)
                for connection in connections: connection.sendall(b'run')
                connection, _ = server.accept()
                connections.append(connection)
                connection.recv(100)
                connection.sendall(b'run')
            except (OSError, TimeoutError) as error:
                errors.append(type(error).__name__)
            finally:
                for connection in connections: connection.close()
                server.close()
        thread = threading.Thread(target=release)
        thread.start()
        captured = io.StringIO()
        try:
            with mock.patch.object(RUNNER, 'ROOT', self.root), contextlib.redirect_stdout(captured):
                code = RUNNER.run_batches(self.batches, dict(self.env, PROBE_FAIL=failure or ''))
        finally:
            thread.join(timeout=6)
        return code, captured.getvalue(), errors

    def test_four_batches_overlap_at_most_three_and_keep_flags_and_output(self):
        code, captured, errors = self.exercise()
        self.assertEqual(code, 0, 'independent batches must overlap and all succeed')
        self.assertEqual(errors, [])
        active, peak, started = set(), 0, []
        for action, name, flags in map(json.loads, (self.root / 'events').read_text().splitlines()):
            self.assertEqual(flags, ['-run', '^TestFixture$'])
            if action == 'start':
                self.assertNotIn(name, started)
                started.append(name)
                active.add(name)
                peak = max(peak, len(active))
            else: active.remove(name)
        self.assertEqual(peak, 3)
        self.assertEqual(active, set())
        self.assertEqual(set(started), {'./one', './two', './three', './four'})
        self.assertEqual([line for line in captured.splitlines() if line.startswith('completed ')],
                         ['completed ./one', 'completed ./two', 'completed ./three', 'completed ./four'])

    def test_failed_batch_stays_red_and_other_batches_still_execute(self):
        code, captured, errors = self.exercise('./four')
        self.assertNotEqual(code, 0)
        self.assertEqual(errors, [])
        for name in ('one', 'two', 'three', 'four'): self.assertIn('completed ./'+name, captured)

    def test_empty_batch_selection_fails(self):
        with self.assertRaisesRegex(ValueError, 'no integration batches'):
            RUNNER.run_batches([], self.env)

    def test_parent_cancellation_waits_for_three_owned_batch_cleanups(self):
        probe = self.root / 'probe.py'
        probe.write_text('''import importlib.util, os, pathlib, signal, sys
spec=importlib.util.spec_from_file_location('runner',sys.argv[1])
runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
runner.ROOT=pathlib.Path(sys.argv[2])
for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,runner.interrupted)
try:
 sys.exit(runner.run_batches([(['./'+name],[]) for name in ('one','two','three','four')],dict(os.environ)))
except runner.IntegrationInterrupted:
 print('batch manager interrupted',flush=True)
 sys.exit(1)
''')
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.root / 'gate'))
            server.listen()
            server.settimeout(5)
            process = subprocess.Popen([sys.executable, '-B', str(probe), RUNNER.__file__, str(self.root)],
                                       env=dict(self.env, PROBE_BLOCK='1'), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, start_new_session=True)
            connections = []
            try:
                names = set()
                for _ in range(3):
                    connection, _ = server.accept()
                    connections.append(connection)
                    names.add(connection.recv(100).decode())
                self.assertEqual(names, {'./one', './two', './three'})
                process.send_signal(signal.SIGTERM)
                stdout, _ = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('batch manager interrupted', stdout)
                for name in ('one', 'two', 'three'):
                    self.assertTrue((self.root / ('clean-'+name)).exists(), 'owned batch cleanup must finish')
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((self.root / ('ready-'+name)).read_text()), 0)
                self.assertFalse((self.root / 'ready-four').exists(), 'queued batches must not start on cancel')
            finally:
                for connection in connections: connection.close()
                for path in self.root.glob('ready-*'):
                    try: os.killpg(int(path.read_text()), signal.SIGKILL)
                    except ProcessLookupError: pass
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()


class ResultsTests(unittest.TestCase):
    def test_prepared_e2e_keeps_ui_build_and_does_not_retry_missing_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'web').mkdir()
            calls = []
            def missing_dependencies(command, **kwargs):
                calls.append(command)
                return 19, 'synthetic missing prepared dependency'
            with mock.patch.object(RUNNER, 'ROOT', root), \
                 mock.patch.object(RUNNER, 'run_command', side_effect=missing_dependencies), \
                 mock.patch.object(RUNNER.signal, 'signal'), \
                 mock.patch.object(sys, 'argv', ['test-e2e.py', '--prepared-web']), \
                 mock.patch.dict(os.environ, {'PATH': os.environ['PATH']}, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'E2E preparation failed'):
                    RUNNER.main(e2e=True)
            self.assertEqual(calls, [['npm', 'run', 'build']])

    def test_e2e_installs_only_its_headless_shell_and_fails_before_database_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'web').mkdir()
            calls = []
            def prepare(command, **kwargs):
                calls.append(command)
                self.assertEqual(kwargs['cwd'], root / 'web')
                return (19, 'synthetic installer failure') if command[0] == 'node' else (0, '')
            with mock.patch.object(RUNNER, 'ROOT', root), \
                 mock.patch.object(RUNNER, 'run_command', side_effect=prepare), \
                 mock.patch.object(RUNNER.signal, 'signal'), \
                 mock.patch.object(sys, 'argv', ['test-e2e.py']), \
                 mock.patch.dict(os.environ, {'PATH': os.environ['PATH']}, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'E2E preparation failed'):
                    RUNNER.main(e2e=True)
            self.assertEqual(calls, [
                ['npm', 'ci'], ['npm', 'run', 'build'],
                ['node', 'node_modules/@playwright/test/cli.js', 'install', '--only-shell', 'chromium'],
            ])

    integration = ("fixture/store", "TestCommitBatch")

    def test_ci_shards_preserve_packages_and_unknown_package_fallback(self):
        self.assertTrue(callable(getattr(RUNNER, 'ci_shards', None)), 'CI needs complete, named integration shards')
        packages = ['./internal/reporting', './internal/jobs', './internal/retention', './internal/store',
                    './internal/api', './internal/new_package', './tests/acceptance']
        shards = RUNNER.ci_shards(packages, ['-race'])
        self.assertEqual(set(shards), {'memory', 'jobs', 'retention', 'api', 'other', 'store'})
        self.assertEqual(shards['memory'], RUNNER.full_suite_batches(packages, ['-race'])[0])
        self.assertEqual(shards['jobs'], (['./internal/jobs'], ['-race']))
        self.assertEqual(shards['retention'], (['./internal/retention'], ['-race']))
        self.assertEqual(shards['api'], (['./internal/api'], ['-race']))
        self.assertNotIn('./internal/api', shards['other'][0])
        self.assertIn('./internal/new_package', shards['other'][0])
        self.assertEqual(shards['store'], (['./internal/store'], ['-race']))
        self.assertEqual(set(p for group, _ in shards.values() for p in group), set(packages))

    def test_ci_shard_cli_is_fixed_and_rejects_target_or_flag_overrides(self):
        self.assertTrue(callable(getattr(RUNNER, 'ci_shard_argument', None)), 'shard selection must fail closed')
        self.assertIsNone(RUNNER.ci_shard_argument(['./...']))
        for name in ('memory', 'jobs', 'retention', 'api', 'other', 'store'):
            self.assertEqual(RUNNER.ci_shard_argument(['--ci-shard', name]), name)
        for argv in (['--ci-shard'], ['--ci-shard', 'unknown'], ['--ci-shard', 'memory', '-timeout', '30m']):
            with self.subTest(argv=argv), self.assertRaises(ValueError): RUNNER.ci_shard_argument(argv)

    def test_unlisted_package_integration_failure_is_actually_red(self):
        self.assert_hidden_go_change_fails('unlisted', 'other')

    def test_api_integration_failure_is_actually_red(self):
        self.assert_hidden_go_change_fails('api', 'api')

    def test_ci_shards_reject_a_missing_api_package(self):
        packages = ['./internal/reporting', './internal/jobs', './internal/retention', './internal/store']
        with self.assertRaisesRegex(ValueError, 'inventory is incomplete'):
            RUNNER.ci_shards(packages, [])

    def assert_hidden_go_change_fails(self, package_name, shard):
        self.assertTrue(callable(getattr(RUNNER, 'ci_shards', None)), 'unknown packages must enter a CI shard')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'go.mod').write_text('module cheesetrap\n\ngo 1.27.4\n')
            packages = ['./internal/reporting', './internal/jobs', './internal/retention', './internal/store',
                        './internal/api', './internal/unlisted']
            for package in packages:
                path = root / package
                path.mkdir(parents=True)
                (path / 'fixture.go').write_text('package ' + path.name + '\n')
            (root / ('internal/' + package_name + '/hidden_test.go')).write_text('//go:build integration\n\npackage ' + package_name + '\n' +
                'import "testing"\nfunc TestHidden(t *testing.T) { t.Fatal("synthetic hidden change") }\n')
            group, flags = RUNNER.ci_shards(packages, [])[shard]
            result = subprocess.run(['go', 'test', '-tags=integration', '-count=1', '-json', *flags, *group],
                cwd=root, env=dict(os.environ, GOENV='off', GOWORK='off', GOFLAGS='', GOTOOLCHAIN='local', GOPROXY='off'),
                capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, 'the real Go assertion must fail')
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            self.assertTrue(any(e.get('Action') == 'fail' and e.get('Package') == 'cheesetrap/internal/' + package_name
                                and e.get('Test') == 'TestHidden' for e in events), result.stdout + result.stderr)

    def test_full_suite_partitions_every_package_and_runs_heavy_case(self):
        packages = ["./internal/api", "./internal/store", "./internal/reporting", "./internal/jobs",
                    "./internal/retention", "./tests/acceptance", "./future/package"]
        batches = RUNNER.full_suite_batches(packages, ["-race"])
        self.assertEqual(len(batches), 4)
        self.assertEqual(set(p for group, _ in batches for p in group), set(packages))
        self.assertEqual(batches[0], (["./internal/reporting"], ["-timeout", "5m", "-race", "-run", "^TestSecurityAggregateMemory$"]))
        self.assertEqual(batches[1], (["./internal/jobs", "./internal/retention"], ["-race"]))
        self.assertEqual(batches[2], (["./internal/api", "./internal/reporting", "./tests/acceptance", "./future/package"],
                                     ["-race", "-skip", "^TestSecurityAggregateMemory$"]))
        self.assertEqual(batches[3], (["./internal/store"], ["-race"]))
        for package in packages:
            groups = [flags for group, flags in batches if package in group]
            self.assertEqual(len(groups), 2 if package == './internal/reporting' else 1,
                             'only reporting may appear twice, with complementary test filters')

    def test_memory_batch_preserves_explicit_timeout(self):
        _, flags = RUNNER.full_suite_batches(["./internal/reporting", "./internal/store", "./internal/jobs",
                                             "./internal/retention"], ["-timeout", "2m"])[0]
        # Go honors the last timeout flag, including a deliberately shorter run.
        self.assertEqual(flags[flags.index("2m") - 1], "-timeout")
        self.assertEqual(flags[-4:], ["-timeout", "2m", "-run", "^TestSecurityAggregateMemory$"])

    def test_full_partition_rejects_incomplete_package_inventory(self):
        packages = ['./internal/jobs', './internal/retention', './internal/reporting', './internal/store']
        for missing in packages:
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'inventory is incomplete'):
                RUNNER.full_suite_batches([p for p in packages if p != missing], [])

    def test_real_discovered_integration_tests_are_selected_exactly_once(self):
        # Discover real source identities independently of the partition. This
        # also covers future packages without adding a package allowlist here.
        _, listed = RUNNER.run_command(['go', 'list', '-f', '{{.Dir}}', './...'], childenv=dict(os.environ))
        packages = ['./' + str(Path(p).relative_to(RUNNER.ROOT)) for p in listed.splitlines()]
        _, output = RUNNER.run_command(['go', 'run', './scripts/list-integration-tests.go', './...'],
                                      childenv=dict(os.environ))
        discovered = json.loads(output)
        self.assertTrue(discovered, 'coverage proof requires actual integration tests')
        self.assertTrue(callable(getattr(RUNNER, 'ci_shards', None)), 'CI shard coverage must match discovery')
        for plan in (RUNNER.full_suite_batches(packages, []), RUNNER.ci_shards(packages, []).values()):
            selected = Counter()
            for group, flags in plan:
                for test in discovered:
                    if './' + test['Package'].removeprefix('cheesetrap/') not in group: continue
                    if '-run' in flags and not re.search(flags[flags.index('-run') + 1], test['Test']): continue
                    if '-skip' in flags and re.search(flags[flags.index('-skip') + 1], test['Test']): continue
                    selected[(test['Package'], test['Test'])] += 1
            self.assertEqual(selected, Counter((test['Package'], test['Test']) for test in discovered))

    def test_bounded_memory_case_can_use_a_separate_owned_run(self):
        packages, flags = RUNNER.arguments(['./...', '-skip', '^TestSecurityAggregateMemory$'])
        self.assertEqual(packages, ['./...'])
        self.assertEqual(flags, ['-skip', '^TestSecurityAggregateMemory$'])
        # Selection never turns an empty or skipped execution into a pass.
        self.assertFalse(RUNNER.results_succeeded(0, set(), [], [], set(), {self.integration}))

    def test_no_test_file_package_skip_does_not_fail_passed_integration(self):
        data = output(
            {"Action": "skip", "Package": "fixture/domain"},
            {"Action": "pass", "Package": "fixture/store", "Test": "TestCommitBatch"},
            {"Action": "pass", "Package": "fixture/store"},
        )
        passed, failed, skipped, no_tests = RUNNER.test_results(data)
        self.assertTrue(RUNNER.results_succeeded(0, passed, failed, skipped, no_tests, {self.integration}))
        self.assertEqual(skipped, [])

    def test_real_test_or_subtest_skip_is_always_failure(self):
        for name in ("TestCommitBatch", "TestCommitBatch/required_case", "TestUnrelatedUnit"):
            data = output(
                {"Action": "skip", "Package": "fixture/store", "Test": name},
                {"Action": "pass", "Package": "fixture/store", "Test": "TestCommitBatch"},
            )
            with self.subTest(name=name):
                self.assertFalse(RUNNER.results_succeeded(0, *RUNNER.test_results(data), {self.integration}))

    def test_no_matching_tests_and_unit_only_cannot_pass(self):
        for data in (output({"Action": "pass", "Package": "fixture/store"}),
                     output({"Action": "pass", "Package": "fixture/store", "Test": "TestUnit"})):
            self.assertFalse(RUNNER.results_succeeded(0, *RUNNER.test_results(data), {self.integration}))

    def test_process_or_package_failure_cannot_pass(self):
        data = output({"Action": "pass", "Package": "fixture/store", "Test": "TestCommitBatch"})
        self.assertFalse(RUNNER.results_succeeded(1, *RUNNER.test_results(data), {self.integration}))
        data += "\n" + output({"Action": "fail", "Package": "fixture/other"})
        self.assertFalse(RUNNER.results_succeeded(0, *RUNNER.test_results(data), {self.integration}))


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
        cls.env.update(GOENV="off", GOFLAGS="", GOWORK="off", GOTOOLCHAIN="local")
        (cls.root / "go.mod").write_text("module fixture\n\ngo 1.27.1\n")
        (cls.root / "fixture.go").write_text("package fixture\n")
        (cls.root / "unit_test.go").write_text('package fixture\nimport "testing"\nfunc TestUnit(t *testing.T) {}\n')
        (cls.root / "integration_test.go").write_text('''//go:build integration

package fixture
import "testing"
func TestCommitBatch(t *testing.T) {}
func Testlowercase(t *testing.T) {}
func TestHelper() {}
type suite struct{}
func (s suite) TestMethod(t *testing.T) {}
''')
        (cls.root / "external_test.go").write_text('''//go:build integration

package fixture_test
import check "testing"
func TestExternal(t *check.T) {}
''')
        (cls.root / "race_test.go").write_text('''//go:build integration && race

package fixture
import "testing"
func TestRaceOnly(t *testing.T) {}
''')
        child = cls.root / "child"
        child.mkdir()
        (child / "child.go").write_text("package child\n")
        (child / "child_test.go").write_text('package child\nimport "testing"\nfunc TestChildUnit(t *testing.T) {}\n')
        cls.helper = Path(__file__).with_name("list-integration-tests.go").resolve()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def discover(self, *args):
        result = subprocess.run(["go", "run", str(self.helper), *args], cwd=self.root, env=self.env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return {(item["Package"], item["Test"]) for item in json.loads(result.stdout)}

    def test_discovers_future_integration_tests_in_both_package_styles(self):
        self.assertEqual(self.discover("./..."), {("fixture", "TestCommitBatch"), ("fixture", "TestExternal")})

    def test_discovery_obeys_race_build_constraint(self):
        self.assertEqual(self.discover("-race", "./..."),
                         {("fixture", "TestCommitBatch"), ("fixture", "TestExternal"), ("fixture", "TestRaceOnly")})

    def test_selected_unit_only_package_has_no_integration_identity(self):
        self.assertEqual(self.discover("./child"), set())


class RealGoResultsTests(unittest.TestCase):
    def test_nested_selection_and_unmatched_unrelated_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go.mod").write_text("module fixture\n\ngo 1.27.1\n")
            (root / "integration_test.go").write_text('''//go:build integration

package fixture
import "testing"
func TestCommitBatch(t *testing.T) { t.Run("actual", func(t *testing.T){}) }
''')
            child = root / "unrelated"
            child.mkdir()
            (child / "unit_test.go").write_text('package unrelated\nimport "testing"\nfunc TestUnit(t *testing.T) {}\n')
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
            env.update(GOENV="off", GOFLAGS="", GOWORK="off", GOTOOLCHAIN="local")
            for selector, accepted in (("TestCommitBatch/no_such_case", False), ("TestCommitBatch/actual", True)):
                with self.subTest(selector=selector):
                    result = subprocess.run(["go", "test", "-tags=integration", "-count=1", "-json", "-run", selector, "./..."],
                                            cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
                    self.assertIn("testing: warning: no tests to run", result.stdout)
                    self.assertEqual(RUNNER.results_succeeded(result.returncode, *RUNNER.test_results(result.stdout),
                                                              {("fixture", "TestCommitBatch")}), accepted)


class SignalTests(unittest.TestCase):
    def test_sigterm_interrupts_real_go_child_and_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, cleanup = root / "ready", root / "cleaned"
            (root / "go.mod").write_text("module signalfixture\n\ngo 1.27.1\n")
            (root / "block_test.go").write_text('''package signalfixture
import ("os"; "strconv"; "testing"; "time")
func TestBlock(t *testing.T) {
 if err:=os.WriteFile(os.Getenv("PROBE_READY")+".tmp",[]byte(strconv.Itoa(os.Getppid())),0600);err!=nil{t.Fatal(err)}
 if err:=os.Rename(os.Getenv("PROBE_READY")+".tmp",os.Getenv("PROBE_READY"));err!=nil{t.Fatal(err)}
 time.Sleep(30*time.Second)
}
''')
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
            env.update(GOENV="off", GOFLAGS="", GOWORK="off", GOTOOLCHAIN="local", PROBE_READY=str(ready))
            script = root / "probe.py"
            script.write_text('''import importlib.util, os, pathlib, signal, sys
spec=importlib.util.spec_from_file_location("runner",sys.argv[1])
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
signal.signal(signal.SIGTERM,runner.interrupted)
try:
 runner.run_command(["go","test","-count=1","-run","TestBlock","."],childenv=dict(os.environ),cwd=sys.argv[2])
except Exception as exc:
 print(str(exc),flush=True)
 sys.exit(1)
finally:
 pathlib.Path(sys.argv[3]).write_text("cleanup finished")
''')
            process = subprocess.Popen([sys.executable, "-B", str(script), str(Path(RUNNER.__file__).resolve()), str(root), str(cleanup)],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            go_group = None
            try:
                deadline = time.monotonic() + 20
                while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "real Go child did not reach its blocking probe")
                go_group = int(ready.read_text())
                process.send_signal(signal.SIGTERM)
                try:
                    stdout, _ = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail("SIGTERM was swallowed while awaiting the child process")
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("integration interrupted", stdout)
                self.assertEqual(cleanup.read_text(), "cleanup finished")
                with self.assertRaises(ProcessLookupError):
                    os.kill(go_group, 0)
            finally:
                # Failure cleanup is scoped to the Go group recorded by this probe.
                if go_group is not None:
                    try:
                        os.killpg(go_group, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()





class E2ESafetyTests(unittest.TestCase):
    def test_external_browser_target_rejected(self):
        for key in ("PLAYWRIGHT_BASE_URL", "CHEESETRAP_E2E_URL", "DATABASE_URL", "DOCKER_HOST"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    RUNNER.validate_environment({key: "http://operational.invalid"})

    def test_empty_environment_allowed(self):
        RUNNER.validate_environment({})



class E2EEntrypointTests(unittest.TestCase):
    def test_rejects_arbitrary_cli_target_before_starting_stack(self):
        for arguments in (['https://operational.invalid'], ['--prepared-web', 'https://operational.invalid']):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(Path(__file__).with_name("test-e2e.py")), *arguments], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("accepts no targets", result.stderr)
                self.assertNotIn("Starting isolated", result.stdout)

    def test_rejects_external_environment_before_install_or_stack(self):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("test-e2e.py"))], env=dict(os.environ, CHEESETRAP_E2E_URL="http://operational.invalid"), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be unset", result.stderr)
        self.assertNotIn("Preparing E2E", result.stdout)



class OwnedCleanupTests(unittest.TestCase):
    def exercise(self, cleanup_code, expected_error="failed, skipped", startup_error=None, test_action="skip"):
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[:2] == ["go", "run"] and "list-integration-tests.go" in command[2]:
                return 0, json.dumps([{"Package": "fixture", "Test": "TestReviewFlow"}])
            if command == ["docker", "context", "show"]: return 0, "local"
            if command[:3] == ["docker", "context", "inspect"]: return 0, '[{"Endpoints":{"docker":{"Host":"unix:///fake.sock"}}}]'
            if command[:2] == ["go", "test"]:
                return 0, output({"Action": test_action, "Package": "fixture", "Test": "TestReviewFlow"})
            return 0, ""
        owner = mock.MagicMock()
        owner.start.return_value = ("a" * 64, "54321")
        owner.start.side_effect = startup_error
        if cleanup_code:
            owner.close.side_effect = RuntimeError("private-cleanup-canary")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "deploy/test-bootstrap.sql").write_text("-- fixture")
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(RUNNER, "OwnedPostgres", return_value=owner), mock.patch.object(RUNNER, "ROOT", root), mock.patch.object(RUNNER, "run_command", side_effect=run), mock.patch.object(RUNNER.signal, "signal"), mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, "argv", ["runner"]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    RUNNER.main()
        self.assertEqual(owner.close.call_count, 1)
        self.assertNotIn("private-cleanup-canary", stdout.getvalue() + stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_skipped_acceptance_fails_and_cleans_exact_owned_project(self):
        self.exercise(0)

    def test_cleanup_error_cannot_be_reported_as_success(self):
        stdout, _ = self.exercise(1, expected_error="cleanup failed", test_action="pass")
        self.assertNotIn("Cleaned isolated project", stdout)

    def test_failed_test_is_preserved_when_cleanup_also_fails(self):
        stdout, stderr = self.exercise(1, test_action="fail")
        self.assertIn("owned integration project cleanup failed", stderr)
        self.assertNotIn("Cleaned isolated project", stdout)

    def test_startup_error_is_preserved_when_cleanup_also_fails(self):
        stdout, stderr = self.exercise(1, expected_error="PostgreSQL startup failed",
                                      startup_error=RuntimeError("Testcontainers PostgreSQL startup failed"))
        self.assertIn("owned integration project cleanup failed", stderr)
        self.assertNotIn("Cleaned isolated project", stdout)

class E2EPreparationSignalTests(unittest.TestCase):
    @staticmethod
    def preparation_fixture(npm, ready):
        pending = ready.with_suffix('.tmp')
        npm.write_text("#!" + sys.executable + "\nimport os, pathlib, time\n" +
                       "pathlib.Path(" + repr(str(pending)) + ").write_text(str(os.getpid()))\n" +
                       "pathlib.Path(" + repr(str(pending)) + ").replace(" + repr(str(ready)) + ")\n" +
                       "time.sleep(30)\n")
        npm.chmod(0o700)

    def test_preparation_readiness_is_published_only_after_pid_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, npm = root / 'ready', root / 'npm'
            self.preparation_fixture(npm, ready)
            opened, release = threading.Event(), threading.Event()
            original_open = Path.open
            errors = []
            def paused_open(path, *args, **kwargs):
                stream = original_open(path, *args, **kwargs)
                mode = args[0] if args else kwargs.get('mode', 'r')
                if path.parent == root and mode == 'w':
                    opened.set()
                    if not release.wait(timeout=2):
                        stream.close()
                        raise RuntimeError('fixture write was not released')
                return stream
            def run_fixture():
                try: runpy.run_path(str(npm), run_name='__main__')
                except Exception as error: errors.append(type(error).__name__)
            with mock.patch.object(Path, 'open', paused_open), mock.patch.object(time, 'sleep'):
                thread = threading.Thread(target=run_fixture)
                thread.start()
                try:
                    self.assertTrue(opened.wait(timeout=2), 'PID writer did not start')
                    self.assertFalse(ready.exists(), 'incomplete PID must not advertise readiness')
                finally:
                    release.set()
                    thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), 'fixture writer did not finish')
            self.assertEqual(errors, [])
            self.assertEqual(int(ready.read_text()), os.getpid())

    def test_sigterm_during_npm_preparation_terminates_owned_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "web").mkdir()
            ready = root / "ready"
            npm = root / "npm"
            self.preparation_fixture(npm, ready)
            probe = root / "probe.py"
            probe.write_text("""import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location("runner",sys.argv[1])
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
runner.ROOT=pathlib.Path(sys.argv[2])
sys.argv=["runner"]
try:
 runner.main(e2e=True)
except runner.IntegrationInterrupted:
 print("preparation interrupted",flush=True)
 sys.exit(1)
""")
            env = {"PATH": str(root), "HOME": directory}
            process = subprocess.Popen([sys.executable, str(probe), RUNNER.__file__, directory], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       start_new_session=True)
            child_group = None
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "preparation child did not start")
                child_group = int(ready.read_text())
                process.send_signal(signal.SIGTERM)
                stdout, _ = process.communicate(timeout=8)
                self.assertNotEqual(process.returncode, 0)
                with self.assertRaises(ProcessLookupError, msg="preparation process group survived cancellation"):
                    os.killpg(child_group, 0)
                self.assertIn("preparation interrupted", stdout)
                self.assertNotIn("Starting isolated", stdout)
            finally:
                # The regression itself must not leave the intentionally blocked
                # preparation child behind when run against the unfixed launcher.
                if child_group is not None:
                    try:
                        os.killpg(child_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()

class TestcontainersLeaseTests(unittest.TestCase):
    def exercise(self, fail=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "helper"
            released = root / "released"
            helper.write_text("#!" + sys.executable + "\n" + """import json, pathlib, sys
ready = pathlib.Path(sys.argv[2])
""" + ("sys.exit(1)\n" if fail else """ready.write_text(json.dumps({"container": "a"*64, "port":"54321"}))
sys.stdin.read()
""" + "pathlib.Path(" + repr(str(released)) + ").touch()\n"))
            helper.chmod(0o700)
            env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
            owner = RUNNER.OwnedPostgres(env, "unix:///fake.sock", "cheesetrap-test-" + "a"*16, "b"*64)
            def build(command, **kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(helper.read_bytes())
                Path(command[command.index("-o") + 1]).chmod(0o700)
                return 0, ""
            with mock.patch.object(RUNNER, "run_command", side_effect=build):
                try:
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "startup failed"):
                            owner.start()
                    else:
                        self.assertEqual(owner.start(), ("a"*64, "54321"))
                        self.assertIsNone(owner.process.poll())
                finally:
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                            owner.close()
                    else:
                        process = owner.process
                        owner.close()
                        self.assertEqual(process.returncode, 0)
                        self.assertTrue(released.exists(), "closing the lease must reach the helper cleanup")

    def test_live_lease_owns_container_until_closed(self):
        self.exercise()

    def test_startup_failure_never_becomes_success(self):
        self.exercise(fail=True)


if __name__ == "__main__":
    unittest.main()
