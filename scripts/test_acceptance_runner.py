import copy
import argparse
import contextlib
import hashlib
import tarfile
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('acceptance_runner', Path(__file__).with_name('test-acceptance.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class AcceptanceGateTests(unittest.TestCase):
    def exercise_initial_preparation(self, *, prepare_web=True, release_sqlc=False, failed='', component=None, stop_at='go'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = root / 'tests/acceptance/coverage.json'
            coverage.parent.mkdir(parents=True)
            coverage.write_bytes((runner.ROOT / 'tests/acceptance/coverage.json').read_bytes())
            toolchain = root / 'deploy/toolchain.lock.json'
            toolchain.parent.mkdir(parents=True)
            toolchain.write_bytes((runner.ROOT / 'deploy/toolchain.lock.json').read_bytes())
            events = []

            def independent(groups, **kwargs):
                events.append([[row['name'] for row in group] for group in groups])
                if component == 'source' and groups[0][0]['name'] == 'runner-tests':
                    self.assertEqual(groups[1][0]['command'], ['./scripts/prepare-ci-web.sh', '--dependencies-only'])
                for group in groups:
                    for row in group:
                        row.update(status='FAIL' if row['name'] == failed else 'PASS',
                                   returncode=9 if row['name'] == failed else 0, process_status='exited')
                        if component == 'browser':
                            (root / row['log']).write_text('')
                        if row['name'] == 'ui' and failed != 'missing-ui-report':
                            report = root / row['report']
                            report.write_text('{' if failed == 'invalid-ui-report' else json.dumps({
                                'success': True, 'testResults': [{'name': 'src/fixture.test.ts', 'status': 'passed',
                                'assertionResults': [{'title': 'fixture', 'fullName': 'fixture', 'status': 'passed'}]}]}))
                if component == 'browser' and not failed:
                    raise runner.AcceptanceTimeout('fixture stops after concurrent browser launch')

            def command(argv, **kwargs):
                events.append(argv)
                if argv[:3] == ['go', 'run', '-p=1'] or argv[0] == './scripts/prepare-ci-web.sh':
                    self.assertEqual(kwargs['env'].get('GOMAXPROCS'), '1', 'SQL preparation keeps its CPU bound')
                else:
                    self.assertNotIn('GOMAXPROCS', kwargs['env'], 'test execution settings stay unchanged')
                if argv[0] == stop_at or (stop_at == 'sql-generate' and argv[-1] == 'generate'):
                    raise runner.AcceptanceTimeout('fixture stops after initial barrier')
                kwargs['log'].write_text('')
                return 9 if argv[0] == './scripts/prepare-ci-web.sh' and failed == 'web-prepare' else 0

            args = argparse.Namespace(prepare_web=prepare_web, release_sqlc=release_sqlc, automated_only=True, linux_evidence=None,
                                      component=component, integration_shard=None)
            with mock.patch.object(runner, 'ROOT', root), \
                 mock.patch.dict(os.environ, {'PATH': os.environ['PATH']}, clear=True), \
                 mock.patch.object(runner.argparse.ArgumentParser, 'parse_args', return_value=args), \
                 mock.patch.object(runner, 'install_signal_handlers'), \
                 mock.patch.object(runner.os, 'umask'), \
                 mock.patch.object(runner, 'checkout_is_clean', return_value=True), \
                 mock.patch.object(runner, 'production_sources', return_value={}), \
                 mock.patch.object(runner, 'go_results', return_value=(set(), [])), \
                 mock.patch.object(runner.subprocess, 'check_output', side_effect=['a' * 40, b'']), \
                 mock.patch.object(runner, 'print_public_summary'), \
                 mock.patch.object(runner, 'run_independent', side_effect=independent), \
                 mock.patch.object(runner, 'run_command', side_effect=command), \
                 contextlib.redirect_stdout(io.StringIO()):
                if component == 'browser' and failed:
                    self.assertEqual(runner.main(), 1)
                else:
                    with self.assertRaises((ValueError, runner.AcceptanceTimeout)):
                        runner.main()
            result = json.loads(next((root / '.local/acceptance').glob('*/results.json')).read_text())
            return events, result

    def test_release_sqlc_only_publishes_a_verified_binary(self):
        script = runner.ROOT / 'scripts/prepare-release-sqlc.sh'
        self.assertTrue(script.is_file(), 'source CI needs a verified sqlc release installer')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            curl = root / 'curl'
            curl.write_text('#!/bin/sh\ncat "$FIXTURE"\n')
            curl.chmod(0o700)
            archive = root / 'fixture.tar.gz'
            binary = root / 'fixture'
            binary.write_text('#!/bin/sh\nprintf "v%s\\n" "$REPORTED_VERSION"\n')
            with tarfile.open(archive, 'w:gz') as tar:
                tar.add(binary, arcname='sqlc')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            for label, reported, expected_hash, ok in (
                ('corrupt', '1.31.1', '0' * 64, False),
                ('wrong-version', '9.9.9', digest, False),
                ('verified', '1.31.1', digest, True),
            ):
                with self.subTest(label=label):
                    out = root / label
                    out.mkdir()
                    env = dict(os.environ, FIXTURE=str(archive), REPORTED_VERSION=reported,
                               PATH=str(root) + os.pathsep + os.environ['PATH'])
                    result = subprocess.run([str(script), str(out), '1.31.1', expected_hash],
                                            env=env, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode == 0, ok)
                    self.assertEqual((out / 'sqlc').exists(), ok)
                    if ok:
                        self.assertEqual((out / 'sqlc').read_bytes(), binary.read_bytes())

    def test_source_generates_sql_with_the_release_it_prepared(self):
        events, result = self.exercise_initial_preparation(component='source', release_sqlc=True,
                                                          stop_at='sql-generate')
        checks = {row['name']: row for row in result['commands']}
        prepare = checks['sql-prepare']['command']
        self.assertEqual(prepare[0], './scripts/prepare-release-sqlc.sh')
        self.assertEqual(events[-1], [str(Path(prepare[1]) / 'sqlc'), 'generate'])
        self.assertEqual(checks['sql-schema']['process_status'], 'timed_out')

    def test_source_component_reaches_ui_without_running_db_harnesses(self):
        events, result = self.exercise_initial_preparation(component='source', stop_at='npm')
        self.assertEqual(events[:-1], [[['runner-tests'], ['web-prepare']],
            ['go', 'test', './...', '-count=1', '-json'], ['go', 'vet', './...'],
            [['ui'], ['sql-prepare']]])
        self.assertEqual(events[-1], ['npm', 'run', 'build'])
        self.assertEqual(result['component'], 'source')

    def test_source_sql_and_ui_keep_independent_failures_and_required_reports(self):
        for failed in ('', 'sql-prepare', 'ui', 'missing-ui-report', 'invalid-ui-report'):
            with self.subTest(failed=failed):
                events, result = self.exercise_initial_preparation(component='source', failed=failed, stop_at='npm')
                self.assertIn([['ui'], ['sql-prepare']], events)
                rows = {r['name']: r for r in result['commands'][:-1]}
                self.assertEqual(rows['sql-prepare']['command'],
                    ['go', 'run', '-p=1', 'github.com/sqlc-dev/sqlc/cmd/sqlc@v1.31.1', 'version'])
                self.assertEqual(rows['ui']['command'][:6],
                    ['npm', 'run', 'test', '--', '--run', '--includeTaskLocation'])
                self.assertEqual(rows['sql-prepare']['status'], 'FAIL' if failed == 'sql-prepare' else 'PASS')
                self.assertEqual(rows['ui']['status'], 'FAIL' if failed in ('ui', 'missing-ui-report', 'invalid-ui-report') else 'PASS')
                self.assertEqual(runner.run_status([], list(rows.values()), True), 'FAIL' if failed else 'PASS_AUTOMATED')

    def test_browser_component_overlaps_web_and_database_preparation(self):
        events, result = self.exercise_initial_preparation(component='browser', stop_at='./scripts/test-e2e.sh')
        self.assertEqual(events, [[['e2e'], ['web-prepare']]])
        web, e2e = result['commands']
        self.assertEqual(web['command'], ['./scripts/prepare-ci-web.sh'])
        self.assertEqual(e2e['command'][:2], ['./scripts/test-e2e.sh', '--prepared-web-status'])
        self.assertEqual(e2e['command'][2], web['completion_status'])
        self.assertTrue(e2e['command'][2].endswith('/web-preparation.status'))
        self.assertEqual(result['component'], 'browser')

    def test_browser_without_preparation_keeps_the_independent_full_e2e_setup(self):
        events, _ = self.exercise_initial_preparation(component='browser', prepare_web=False, stop_at='./scripts/test-e2e.sh')
        self.assertEqual(events, [['./scripts/test-e2e.sh']])

    def test_browser_preparation_failure_keeps_both_results_without_e2e_install_retry(self):
        events, result = self.exercise_initial_preparation(component='browser', failed='web-prepare',
                                                          stop_at='./scripts/test-e2e.sh')
        self.assertEqual(events, [[['e2e'], ['web-prepare']]])
        self.assertEqual([(r['name'], r['status']) for r in result['commands']], [('web-prepare', 'FAIL'), ('e2e', 'FAIL')])
        self.assertEqual(result['status'], 'FAIL')

    def test_parallel_web_completion_releases_database_lane_only_on_success(self):
        for exit_code in (0, 9):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                status, database, tested = root / 'web.status', root / 'database', root / 'tested'
                def record(name, code, *args):
                    return dict(name=name, command=[sys.executable, '-c', code, *map(str, args)],
                                cwd=root, log=name+'.log', timeout=5, status='FAIL')
                consumer = record('e2e', '''import pathlib,sys,time
status,database,tested=map(pathlib.Path,sys.argv[1:])
database.touch()
deadline=time.monotonic()+2
while not status.exists() and time.monotonic()<deadline: time.sleep(.01)
if not status.exists() or status.read_text()!='PASS\\n': sys.exit(11)
tested.touch()
''', status, database, tested)
                producer = record('web-prepare', '''import pathlib,sys,time
database=pathlib.Path(sys.argv[1])
deadline=time.monotonic()+2
while not database.exists() and time.monotonic()<deadline: time.sleep(.01)
sys.exit(int(sys.argv[2]) if database.exists() else 12)
''', database, exit_code)
                producer['completion_status'] = str(status)
                with mock.patch.object(runner, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                    runner.run_independent([[consumer], [producer]], env=dict(PATH=os.environ['PATH']))
                self.assertEqual(producer['returncode'], exit_code)
                self.assertEqual(consumer['returncode'], 0 if exit_code == 0 else 11)
                self.assertTrue(status.is_file(), 'the producer must publish its actual terminal result')
                self.assertEqual(status.read_text(), 'PASS\n' if exit_code == 0 else 'FAIL\n')
                self.assertEqual(status.stat().st_mode & 0o777, 0o600)
                self.assertEqual(tested.exists(), exit_code == 0)

    def test_stale_completion_status_cannot_release_a_new_browser_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / 'web.status'
            status.write_text('PASS\n')
            row = dict(name='web-prepare', command=['unexpected'], cwd=root, log='web.log',
                       timeout=5, status='FAIL', completion_status=str(status))
            with mock.patch.object(runner, 'ROOT', root), \
                 mock.patch.object(runner.asyncio, 'create_subprocess_exec') as start:
                with self.assertRaisesRegex(ValueError, 'already exists'):
                    runner.run_independent([[dict(row, name='e2e', completion_status=None)], [row]], env={})
                start.assert_not_called()
            self.assertEqual(status.read_text(), 'PASS\n')

    def test_integration_component_only_runs_the_complete_existing_harness(self):
        events, result = self.exercise_initial_preparation(component='integration', prepare_web=False,
                                                          stop_at='./scripts/test-integration.sh')
        self.assertEqual(events, [['./scripts/test-integration.sh', './...']])
        self.assertEqual(result['component'], 'integration')

    def test_component_requirements_partition_all_tests_without_a_coverage_gap(self):
        manifest = json.loads((runner.ROOT / 'tests/acceptance/coverage.json').read_text())
        suites = {name: set() for name in ('go', 'integration', 'e2e', 'browser', 'ui')}
        identities = []
        for row in manifest['requirements']:
            for test in row['tests']:
                identity = (test['package'], test['name'])
                suites[test['suite']].add(identity)
                identities.append((test['suite'], identity))
            for name in ('browser', 'ui'):
                for test in row.get(name + '_tests', []):
                    identity = (test['file'], test['name'])
                    suites[name].add(identity)
                    identities.append((name, identity))
        self.assertTrue(callable(getattr(runner, 'evaluate_component', None)),
                        'component gates must validate their concrete coverage')
        for name in ('source', 'browser', 'integration'):
            rows = runner.evaluate_component(manifest, suites, name)
            self.assertEqual({r['id'] for r in rows}, {r['id'] for r in manifest['requirements']})
            self.assertFalse(any(r['status'] == 'FAIL' for r in rows))
        for suite, identity in set(identities):
            partial = copy.deepcopy(suites)
            partial[suite].remove(identity)
            with self.subTest(suite=suite, identity=identity):
                owners = [name for name in ('source', 'browser', 'integration')
                          if any(r['status'] == 'FAIL' for r in runner.evaluate_component(manifest, partial, name))]
                self.assertEqual(owners, ['integration' if suite == 'integration' else
                                          'browser' if suite in ('e2e', 'browser') else 'source'])
                self.assertTrue(any(r['status'] == 'FAIL' for r in runner.evaluate(manifest, partial, {})))

    def test_required_integration_entries_belong_to_one_ci_shard(self):
        manifest = json.loads((runner.ROOT / 'tests/acceptance/coverage.json').read_text())
        self.assertTrue(callable(getattr(runner, 'integration_shard_for', None)), 'shard requirement ownership must be explicit')
        suites = {'integration': {(t['package'], t['name']) for row in manifest['requirements']
                                  for t in row['tests'] if t['suite'] == 'integration'}}
        self.assertEqual(runner.integration_shard_for(dict(package='cheesetrap/internal/api', name='TestReviewFlow')), 'api')
        self.assertEqual(runner.integration_shard_for(dict(package='cheesetrap/internal/jobs', name='TestRetentionRiverDeferralRecovery')), 'retention-recovery')
        names = ('memory', 'jobs', 'retention-recovery', 'retention', 'api', 'other', 'store')
        for identity in suites['integration']:
            partial = {'integration': suites['integration'] - {identity}}
            owners = [name for name in names if any(row['status'] == 'FAIL' for row in
                      runner.evaluate_component(manifest, partial, 'integration', name))]
            self.assertEqual(len(owners), 1, 'no missing requirement may fall between shards')
            self.assertEqual(owners[0], runner.integration_shard_for(dict(package=identity[0], name=identity[1])))
        self.assertEqual(runner.integration_shard_for(dict(package='cheesetrap/internal/new_package', name='TestNew')), 'other')
        self.assertEqual(runner.integration_shard_for(dict(package='cheesetrap/internal/reporting',
                         name='TestSecurityAggregateMemory')), 'memory')

    def test_component_cli_and_gate_preserve_real_process_and_evidence_failures(self):
        spec = importlib.util.spec_from_file_location('component_release', runner.ROOT / 'scripts/release-check.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in set(checker.metadata_inputs(runner.ROOT)) | {'.gitignore', 'scripts/test-acceptance.py', 'scripts/release-check.py'}:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((runner.ROOT / name).read_bytes())
            manifest = dict(schema=1, requirements=[dict(id=f'T{i:02}', linux=[], tests=[
                dict(suite='integration', package='cheesetrap/internal/fixture', name='TestFixture')])
                for i in range(1, 14)])
            (root / 'tests/acceptance/coverage.json').write_text(json.dumps(manifest))
            harness = root / 'scripts/test-integration.sh'
            harness.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parents[1]
sharded = sys.argv[1:] in (['--ci-shard', 'memory'], ['--ci-shard', 'retention-recovery'])
recovery = sys.argv[1:] == ['--ci-shard', 'retention-recovery']
assert sharded or sys.argv[1:] == ['./...']
mode = (root/'fixture-mode').read_text()
for batch in range(1 if sharded else 4):
 path = root/'.local/fixture'/('batch'+str(batch)+'.jsonl')
 path.parent.mkdir(parents=True, exist_ok=True)
 event = dict(Action='fail' if mode=='failed-test' and batch==2 else 'pass',
              Package='cheesetrap/internal/jobs' if recovery else 'cheesetrap/internal/reporting' if sharded else 'cheesetrap/internal/fixture',
              Test=('TestRetentionRiverDeferralRecovery' if mode!='wrong-recovery-test' else 'TestFixture') if recovery else 'TestSecurityAggregateMemory' if sharded and mode!='wrong-memory-test' else 'TestFixture')
 output = dict(Action='output', Package=event['Package'], Test=event['Test'], Output='private-runtime-canary')
 path.write_text(json.dumps(event)+'\\n'+json.dumps(output)+'\\n')
 if not (mode=='missing-evidence' and batch==3): print('PASS: evidence '+str(path.relative_to(root)))
sys.exit(9 if mode=='process-failed' else 0)
''')
            harness.chmod(0o700)
            env = dict(PATH=os.environ['PATH'], GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            def git(*arguments):
                subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *arguments],
                               cwd=root, env=env, check=True, capture_output=True, timeout=10)
            git('init', '-q')
            for mode, shard in [('good', None), ('failed-test', None), ('missing-evidence', None),
                                ('process-failed', None), ('good', 'memory'), ('wrong-memory-test', 'memory'),
                                ('good', 'retention-recovery'), ('wrong-recovery-test', 'retention-recovery')]:
                with self.subTest(mode=mode, shard=shard):
                    (root / 'fixture-mode').write_text(mode)
                    git('add', '.')
                    git('commit', '-qm', 'fixture ' + mode)
                    result = subprocess.run([sys.executable, '-B', str(root / 'scripts/test-acceptance.py'),
                        '--automated-only', '--component', 'integration',
                        *(['--integration-shard', shard] if shard else [])], cwd=root, env=env,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0 if mode == 'good' else 1, result.stdout + result.stderr)
                    self.assertNotIn('private-runtime-canary', result.stdout + result.stderr)
                    path = max((root / '.local/acceptance').glob('*/results.json'))
                    report = json.loads(path.read_text())
                    self.assertEqual(report['status'], 'PASS_COMPONENT' if mode == 'good' else 'FAIL')
                    self.assertEqual(report['component'], 'integration')
                    self.assertEqual(report.get('integration_shard'), shard)
                    self.assertEqual([c['name'] for c in report['commands']], ['integration'])
                    gate = subprocess.run([sys.executable, '-B', str(root / 'scripts/release-check.py'),
                        '--component-results', 'integration', str(path),
                        *(['--integration-shard', shard] if shard else [])], cwd=root, env=env,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(gate.returncode, 0 if mode == 'good' else 1, gate.stdout + gate.stderr)
                    if mode == 'good':
                        full = subprocess.run([sys.executable, '-B', str(root / 'scripts/release-check.py'),
                            '--acceptance-results', str(path)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(full.returncode, 1, 'partial evidence must never pass complete acceptance')

    def test_browser_cli_and_gate_reject_process_test_and_browser_evidence_failures(self):
        spec = importlib.util.spec_from_file_location('browser_release', runner.ROOT / 'scripts/release-check.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in set(checker.metadata_inputs(runner.ROOT)) | {'.gitignore', 'scripts/test-acceptance.py', 'scripts/release-check.py'}:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((runner.ROOT / name).read_bytes())
            manifest = dict(schema=1, requirements=[dict(id=f'T{i:02}', linux=[], tests=[
                dict(suite='e2e', package='cheesetrap/internal/fixture', name='TestFixture')],
                browser_tests=[dict(file='e2e/fixture.spec.ts', name='fixture journey')]) for i in range(1, 14)])
            (root / 'tests/acceptance/coverage.json').write_text(json.dumps(manifest))
            harness = root / 'scripts/test-e2e.sh'
            harness.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parents[1]
assert sys.argv[1:] == []
mode = (root/'fixture-mode').read_text()
path = root/'.local/fixture/e2e.jsonl'
path.parent.mkdir(parents=True, exist_ok=True)
event = dict(Action='fail' if mode=='failed-test' else 'pass', Package='cheesetrap/internal/fixture',
             Test='TestOther' if mode=='missing-go' else 'TestFixture')
path.write_text(json.dumps(event)+'\\n'+json.dumps(dict(Action='output', Package=event['Package'],
                Test=event['Test'], Output='private-runtime-canary'))+'\\n')
if mode!='missing-evidence': print('PASS: evidence '+str(path.relative_to(root)))
browser = root/'.local/e2e/browser-results.json'
browser.parent.mkdir(parents=True, exist_ok=True)
if mode!='missing-browser':
 results = [dict(status='skipped' if mode=='skipped-browser' else 'passed')]
 if mode=='retried-browser': results.append(dict(status='passed'))
 browser.write_text(json.dumps(dict(specs=[dict(file='e2e/fixture.spec.ts', title='fixture journey',
      tests=[dict(expectedStatus='passed', status='expected', results=results)])])))
sys.exit(9 if mode=='process-failed' else 0)
''')
            harness.chmod(0o700)
            env = dict(PATH=os.environ['PATH'], GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            def git(*arguments):
                subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', *arguments],
                               cwd=root, env=env, check=True, capture_output=True, timeout=10)
            git('init', '-q')
            for mode in ('good', 'failed-test', 'missing-evidence', 'process-failed', 'skipped-browser',
                         'retried-browser', 'missing-browser', 'missing-go'):
                with self.subTest(mode=mode):
                    (root / 'fixture-mode').write_text(mode)
                    git('add', '.')
                    git('commit', '-qm', 'fixture ' + mode)
                    result = subprocess.run([sys.executable, '-B', str(root / 'scripts/test-acceptance.py'),
                        '--automated-only', '--component', 'browser'], cwd=root, env=env,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0 if mode == 'good' else 1, result.stdout + result.stderr)
                    self.assertNotIn('private-runtime-canary', result.stdout + result.stderr)
                    path = max((root / '.local/acceptance').glob('*/results.json'))
                    report = json.loads(path.read_text())
                    self.assertEqual(report['status'], 'PASS_COMPONENT' if mode == 'good' else 'FAIL')
                    self.assertEqual(report['component'], 'browser')
                    self.assertEqual([c['name'] for c in report['commands']], ['e2e'])
                    gate = subprocess.run([sys.executable, '-B', str(root / 'scripts/release-check.py'),
                        '--component-results', 'browser', str(path)], cwd=root, env=env,
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(gate.returncode, 0 if mode == 'good' else 1, gate.stdout + gate.stderr)
                    if mode == 'good':
                        full = subprocess.run([sys.executable, '-B', str(root / 'scripts/release-check.py'),
                            '--acceptance-results', str(path)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=10)
                        self.assertEqual(full.returncode, 1, 'browser evidence cannot pass complete acceptance')

    def test_ci_initial_preparation_joins_both_results_before_go(self):
        for failed in ('', 'runner-tests'):
            with self.subTest(failed=failed):
                events, result = self.exercise_initial_preparation(failed=failed)
                self.assertEqual(events, [[['runner-tests'], ['web-prepare']],
                                          ['go', 'test', './...', '-count=1', '-json']])
                statuses = {row['name']: row['status'] for row in result['commands']}
                self.assertEqual(statuses['web-prepare'], 'PASS')
                self.assertEqual(statuses['runner-tests'], 'FAIL' if failed else 'PASS')
                self.assertIs(result['prepare_web'], True)
                if failed:
                    self.assertEqual(runner.run_status([], result['commands'][:2], True), 'FAIL')

    def test_failed_web_preparation_aborts_without_install_retry_or_e2e(self):
        events, result = self.exercise_initial_preparation(failed='web-prepare')
        self.assertEqual(events, [[['runner-tests'], ['web-prepare']]])
        self.assertEqual([(r['name'], r['status']) for r in result['commands']],
                         [('runner-tests', 'PASS'), ('web-prepare', 'FAIL')])
        self.assertEqual(result['status'], 'FAIL')
        self.assertIs(result['aborted'], True)
        self.assertIn('web preparation failed', result['reason'])

    def test_local_prepared_acceptance_keeps_existing_initial_commands(self):
        events, result = self.exercise_initial_preparation(prepare_web=False)
        self.assertEqual(events, [['python3', '-B', 'scripts/test-python.py', 'regression'],
                                  ['go', 'test', './...', '-count=1', '-json']])
        self.assertNotIn('web-prepare', [r['name'] for r in result['commands']])

    def test_web_preparation_requires_explicit_ci_mode(self):
        for arguments in ([], ['--linux-evidence', '/nonexistent/fixture.json']):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, '-B', str(runner.ROOT / 'scripts/test-acceptance.py'),
                                         '--prepare-web', *arguments], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--prepare-web requires --automated-only', result.stderr)

    def test_component_modes_are_explicit_and_do_not_prepare_unrelated_tools(self):
        for args, message in [(['--component', 'source'], '--component requires --automated-only'),
                              (['--automated-only', '--component', 'source', '--integration-shard', 'memory'],
                               '--integration-shard requires the integration component'),
                              (['--automated-only', '--component', 'integration', '--prepare-web'],
                               'integration component cannot prepare web dependencies')]:
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, '-B', str(runner.ROOT / 'scripts/test-acceptance.py'),
                                         *args], capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_web_preparation_public_verdict_cannot_hide_failure(self):
        output = io.StringIO()
        with mock.patch.object(runner.subprocess, 'check_output', return_value=b''), \
             contextlib.redirect_stdout(output):
            runner.print_public_summary([dict(name='web-prepare', status='FAIL')])
        self.assertEqual(json.loads(output.getvalue().removeprefix('CI_CHECK ')),
                         dict(command='web-prepare', status='FAIL', categories=[], locations=[], tests=[]))

    def test_locked_web_setup_order_and_failure_stop_without_retry(self):
        script = runner.ROOT / 'scripts/prepare-ci-web.sh'
        self.assertTrue(script.is_file(), 'CI web setup needs an owned fail-fast command')
        cases = [([], '', ['npm', 'npx'], 0), ([], 'npm', ['npm'], 9), ([], 'npx', ['npm', 'npx'], 9),
                 (['--dependencies-only'], '', ['npm'], 0), (['--dependencies-only'], 'npm', ['npm'], 9),
                 (['--unknown'], '', [], 2)]
        for arguments, failed, expected, code in cases:
            with self.subTest(failed=failed), tempfile.TemporaryDirectory(prefix='ci web ') as directory:
                root = Path(directory)
                (root / 'bin').mkdir()
                (root / 'scripts').mkdir()
                (root / 'web').mkdir()
                target = root / 'scripts/prepare-ci-web.sh'
                target.write_bytes(script.read_bytes())
                for name in ('npm', 'npx'):
                    executable = root / 'bin' / name
                    executable.write_text('#!' + sys.executable + '\n' +
                        'import json, os, pathlib, sys\n' +
                        'name = pathlib.Path(sys.argv[0]).name\n' +
                        'with open(os.environ["EVENTS"], "a") as stream:\n' +
                        ' stream.write(json.dumps([name, os.getcwd(), sys.argv[1:]])+"\\n")\n' +
                        'sys.exit(9 if os.environ["FAIL"] == name else 0)\n')
                    executable.chmod(0o700)
                result = subprocess.run(['sh', str(target), *arguments], cwd='/', capture_output=True, text=True,
                    timeout=5, env=dict(PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'],
                                        EVENTS=str(root / 'events'), FAIL=failed))
                self.assertEqual(result.returncode, code, result.stderr)
                rows = list(map(json.loads, (root / 'events').read_text().splitlines())) if (root / 'events').exists() else []
                self.assertEqual([r[0] for r in rows], expected)
                self.assertTrue(all(r[1] == str(root / 'web') for r in rows))
                if rows: self.assertEqual(rows[0][2], ['ci', '--no-audit', '--no-fund'])
                if len(rows) == 2:
                    self.assertEqual(rows[1][2], ['--no-install', 'playwright', 'install', '--with-deps', '--only-shell', 'chromium'])

    def test_memory_timings_are_bounded_private_and_never_change_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            source = 'internal/reporting/memory_integration_test.go'
            (root / source).parent.mkdir(parents=True)
            (root / source).write_text('package reporting\nfunc TestSecurityAggregateMemory(t *testing.T) {}\n')
            def event(row, **changes):
                return dict(dict(Action='output', Package='cheesetrap/internal/reporting',
                                 Test='TestSecurityAggregateMemory',
                                 Output='    memory_integration_test.go:150: MEMORY_TIMING '+json.dumps(row)+'\n'), **changes)
            valid = dict(phase='child_build', elapsed_ms=1250, compile_actions=45)
            rows = [event(valid), event(dict(phase='draft', elapsed_ms=2500)),
                    event(dict(phase='revision', elapsed_ms=1)), event(dict(phase='revision', elapsed_ms=2)),
                    event(dict(valid, phase='private-runtime-canary')),
                    event(dict(valid, elapsed_ms=True)), event(dict(valid, elapsed_ms=-1)),
                    event(dict(valid, elapsed_ms=1200001)), event(dict(valid, compile_actions=True)),
                    event(dict(valid, compile_actions=10001)), event(dict(valid, compile_actions=-1)),
                    event(dict(valid, extra='private-runtime-canary')),
                    event(dict(valid, phase='draft')), event(valid, Action='pass'),
                    event(valid, Package='private-runtime-canary'),
                    event(valid, Test='TestSecurityAggregateMemory/private-runtime-canary'),
                    event(valid, Output='MEMORY_TIMING not-json'), event(valid, Output=None),
                    event(dict(phase=[], elapsed_ms=1)), event(dict(phase='draft', elapsed_ms=1.5)),
                    dict(Action='output', Output='private-runtime-canary'), [], None]
            (root / '.local/batch.jsonl').write_text('\n'.join(map(json.dumps, rows)))
            (root / 'outside.jsonl').write_text(json.dumps(event(dict(phase='wide_revision', elapsed_ms=99))))
            (root / '.local/alias.jsonl').symlink_to(root / 'outside.jsonl')
            for status in ('PASS', 'FAIL'):
                captured = io.StringIO()
                with self.subTest(status=status), mock.patch.object(runner, 'ROOT', root), \
                     mock.patch.object(runner.subprocess, 'check_output', return_value=(source+'\0').encode()), \
                     contextlib.redirect_stdout(captured):
                    runner.print_public_summary([dict(name='integration', status=status,
                        test_logs=['.local/batch.jsonl', '.local/../outside.jsonl', '.local/alias.jsonl'])])
                row = json.loads(captured.getvalue().removeprefix('CI_CHECK '))
                self.assertEqual(row.get('memory_timings'), [
                    dict(batch=1, **valid), dict(batch=1, phase='draft', elapsed_ms=2500)])
                self.assertEqual(row['status'], status)
                self.assertNotIn('private-runtime-canary', captured.getvalue())
                self.assertNotIn('outside.jsonl', captured.getvalue())
            with mock.patch.object(runner, 'ROOT', root):
                self.assertEqual(len(runner.public_memory_timings(['.local/batch.jsonl'] * 10)), 11)

    def test_package_timings_publish_only_source_packages_and_never_change_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            source = 'internal/known/fixture_test.go'
            (root / source).parent.mkdir(parents=True)
            (root / source).write_text('package known\nfunc TestKnown(t *testing.T) {}\n')
            package = 'cheesetrap/internal/known'
            event = dict(Action='pass', Package=package, Elapsed=1.25)
            (root / '.local/batch1.jsonl').write_text('\n'.join(map(json.dumps, [
                event, dict(event, Test='TestKnown/private-runtime-canary', Elapsed=0.1),
                dict(event, Package='cheesetrap/private-runtime-canary'),
                dict(Action='output', Package=package, Output='private-runtime-canary'),
                dict(event, Elapsed=True), dict(event, Elapsed=-1), dict(event, Elapsed=1200.1),
                dict(event, Elapsed=float('nan')), dict(event, Elapsed=float('inf')),
                ['private-runtime-canary'], None,
            ])))
            (root / '.local/batch2.jsonl').write_text(json.dumps(dict(event, Elapsed=2.5)))
            (root / 'outside.jsonl').write_text(json.dumps(dict(event, Elapsed=9)))
            (root / '.local/alias.jsonl').symlink_to(root / 'outside.jsonl')
            (root / '.local/integration.log').write_text('owned integration project cleanup failed\n')
            for status in ('PASS', 'FAIL'):
                captured = io.StringIO()
                with self.subTest(status=status), mock.patch.object(runner, 'ROOT', root), \
                     mock.patch.object(runner.subprocess, 'check_output', return_value=(source+'\0').encode()), \
                     contextlib.redirect_stdout(captured):
                    runner.print_public_summary([{
                        'name': 'integration', 'status': status, 'log': '.local/integration.log',
                        'test_logs': ['.local/batch1.jsonl', '.local/batch2.jsonl',
                                      '.local/../outside.jsonl', '.local/alias.jsonl'],
                    }])
                row = json.loads(captured.getvalue().removeprefix('CI_CHECK '))
                self.assertEqual(row.get('package_timings'), [
                    dict(batch=1, package=package, elapsed_ms=1250),
                    dict(batch=2, package=package, elapsed_ms=2500),
                ])
                self.assertEqual(row['status'], status)
                self.assertNotIn('private-runtime-canary', captured.getvalue())
                self.assertNotIn('outside.jsonl', captured.getvalue())

    def test_package_timings_omit_ambiguity_and_bound_public_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            sources = {f'internal/p{n:02}/fixture.go': '' for n in range(66)}
            events = [dict(Action='pass', Package=f'cheesetrap/internal/p{n:02}', Elapsed=0)
                      for n in range(66)]
            events += [dict(events[0], Elapsed=1), dict(events[1], Elapsed=2)]
            (root / '.local/batch.jsonl').write_text('\n'.join(map(json.dumps, events)))
            with mock.patch.object(runner, 'ROOT', root):
                rows = runner.public_package_timings(['.local/batch.jsonl'], sources)
                self.assertEqual(rows, [
                    dict(batch=1, package=f'cheesetrap/internal/p{n:02}', elapsed_ms=0)
                    for n in range(2, 66)
                ])
                # A second valid batch cannot grow the public diagnostic unboundedly.
                self.assertEqual(runner.public_package_timings(
                    ['.local/batch.jsonl', '.local/batch.jsonl'], sources), rows)
                self.assertEqual(runner.public_package_timings(None, sources), [])
                self.assertEqual(runner.public_package_timings(['.local/missing.jsonl'], sources), [])

    def test_aborted_queued_commands_without_logs_still_report_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = io.StringIO()
            with mock.patch.object(runner, 'ROOT', root), \
                 mock.patch.object(runner.subprocess, 'check_output', return_value=b''), \
                 contextlib.redirect_stdout(captured):
                runner.print_public_summary([{'name': name, 'status': 'FAIL', 'log': '.local/absent.log'}
                                             for name in ('integration', 'go')])
            rows = [json.loads(line.removeprefix('CI_CHECK ')) for line in captured.getvalue().splitlines()]
            self.assertEqual([(row['command'], row['status']) for row in rows],
                             [('integration', 'FAIL'), ('go', 'FAIL')])

    def test_integration_summary_exposes_only_bounded_phase_timings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            rows = [
                {'phase': 'migrations', 'elapsed_ms': 1500},
                {'phase': 'tests', 'elapsed_ms': 2300},
                {'phase': 'private-runtime-canary', 'elapsed_ms': 10},
                {'phase': 'tests', 'elapsed_ms': True},
                {'phase': 'tests', 'elapsed_ms': -1},
                {'phase': 'tests', 'elapsed_ms': 1200001},
                {'phase': 'tests', 'elapsed_ms': 10, 'private-runtime-canary': 'secret'},
            ]
            (root / '.local/integration.log').write_text('\n'.join(
                'INTEGRATION_TIMING ' + json.dumps(row) for row in rows))
            captured = io.StringIO()
            with mock.patch.object(runner, 'ROOT', root), \
                 mock.patch.object(runner.subprocess, 'check_output', return_value=b''), \
                 contextlib.redirect_stdout(captured):
                runner.print_public_summary([{
                    'name': 'integration', 'status': 'PASS', 'log': '.local/integration.log',
                }])
            row = json.loads(captured.getvalue().removeprefix('CI_CHECK '))
            self.assertEqual(row, {'command': 'integration', 'status': 'PASS', 'timings': rows[:2]})
            self.assertNotIn('private-runtime-canary', captured.getvalue())
            self.assertEqual(runner.public_integration_timings('INTEGRATION_TIMING not-json\n'), [])
            repeated = '\n'.join('INTEGRATION_TIMING ' + json.dumps(rows[0]) for _ in range(25))
            self.assertEqual(len(runner.public_integration_timings(repeated)), 20)

    def test_generated_snapshot_detects_model_changes_additions_and_deletions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'web/src/api/generated/models.ts'
            model.parent.mkdir(parents=True)
            model.write_text('export type Model = string;')
            with mock.patch.object(runner, 'ROOT', root):
                before = runner.generated_snapshot()
                model.write_text('export type Model = number;')
                self.assertNotEqual(before, runner.generated_snapshot())
                model.write_text('export type Model = string;')
                self.assertEqual(before, runner.generated_snapshot())
                extra = model.with_name('extra.ts')
                extra.write_text('export type Extra = string;')
                self.assertNotEqual(before, runner.generated_snapshot())
                extra.unlink()
                model.unlink()
                self.assertNotEqual(before, runner.generated_snapshot())

    def test_summary_retains_runner_failures_after_successful_batch_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            (root / '.local/passed.jsonl').write_text(json.dumps({
                'Action': 'pass', 'Package': 'cheesetrap/example', 'Test': 'TestPassed',
            }) + '\n')
            (root / '.local/integration.log').write_text(
                'PASS: 1 tests; evidence .local/passed.jsonl\n'
                'Testcontainers PostgreSQL startup failed\n'
                'owned integration project cleanup failed\n'
                'private-runtime-canary\n')
            captured = io.StringIO()
            with mock.patch.object(runner, 'ROOT', root), \
                 mock.patch.object(runner.subprocess, 'check_output', return_value=b''), \
                 contextlib.redirect_stdout(captured):
                runner.print_public_summary([{
                    'name': 'integration', 'status': 'FAIL', 'log': '.local/integration.log',
                }])
            row = json.loads(captured.getvalue().removeprefix('CI_CHECK '))
            self.assertEqual(row, {'command': 'integration', 'status': 'FAIL',
                                  'categories': ['cleanup_failed', 'postgres_startup_failed'],
                                  'tests': [], 'locations': []})
            self.assertNotIn('private-runtime-canary', captured.getvalue())

    def test_harness_merges_four_isolated_batches_and_single_e2e(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            logs, expected = [], set()
            for n in range(4):
                identity = ('cheesetrap/example', f'TestBatch{n}')
                expected.add(identity)
                relative = f'.local/batch{n}.jsonl'
                (root / relative).write_text(json.dumps(dict(Action='pass', Package=identity[0], Test=identity[1])))
                logs.append(relative)
            output = '\n'.join('PASS: 1 tests; evidence ' + path for path in logs)
            self.assertEqual(runner.harness_results(output, 0, 4, root), (expected, logs))
            self.assertEqual(runner.harness_results('evidence ' + logs[0], 0, 1, root),
                             ({('cheesetrap/example', 'TestBatch0')}, logs[:1]))
            for case in ('failed-exit', 'missing-batch', 'duplicate', 'missing-file', 'empty', 'fail', 'skip', 'escape'):
                with self.subTest(case=case):
                    changed, code = output, 0
                    path = root / logs[-1]
                    original = path.read_text()
                    if case == 'failed-exit': code = 1
                    if case == 'missing-batch': changed = '\n'.join(output.splitlines()[:-1])
                    if case == 'duplicate': changed = output.replace(logs[-1], logs[0])
                    if case == 'missing-file': path.unlink()
                    if case == 'empty': path.write_text('')
                    if case in ('fail', 'skip'):
                        path.write_text(original + '\n' + json.dumps(dict(Action=case, Package='cheesetrap/example', Test='TestBad')))
                    if case == 'escape':
                        (root / 'outside.jsonl').write_text(original)
                        changed = output.replace(logs[-1], '.local/../outside.jsonl')
                    with self.assertRaises(ValueError):
                        runner.harness_results(changed, code, 4, root)
                    path.write_text(original)

    def test_public_diagnostics_never_echo_runtime_values(self):
        sources = {'internal/example/check_test.go': 'package example\nfunc TestCheck(t *testing.T) {\n}\n',
                   'scripts/test_fixture.py': 'def test_fixture(self):\n    pass\n',
                   'internal/unrelated/check_test.go': 'package unrelated\nfunc TestOther(t *testing.T) {\n}\n'}
        secret = 'runtime-secret-value'
        output = '\n'.join([
            json.dumps({'Action': 'fail', 'Package': 'cheesetrap/internal/example',
                        'Test': 'TestCheck/' + secret, 'Output': secret}),
            '    check_test.go:3: password=' + secret,
            '    FAIL: test_fixture (fixture.Tests.test_fixture)',
            'context deadline exceeded: https://user:' + secret + '@private.invalid',
            '--- FAIL: TestInvented' + secret,
            'File "/private/' + secret + '/test_fixture.py", line 2',
        ])
        summary = runner.public_diagnostics(output, sources)
        self.assertNotIn(secret, json.dumps(summary))
        self.assertNotIn('private.invalid', json.dumps(summary))
        self.assertEqual(summary['tests'], ['internal/example/check_test.go::TestCheck',
                                            'scripts/test_fixture.py::test_fixture'])
        self.assertEqual(summary['locations'], ['internal/example/check_test.go:3',
                                                'scripts/test_fixture.py:2'])
        self.assertIn('deadline_exceeded', summary['categories'])

    def test_public_diagnostics_reject_unknown_names_and_out_of_range_lines(self):
        sources = {'internal/check_test.go': 'func TestKnown(t *testing.T) {}\n'}
        summary = runner.public_diagnostics('--- FAIL: TestUnknown\ncheck_test.go:999: token\n', sources)
        self.assertEqual(summary['tests'], [])
        self.assertEqual(summary['locations'], [])
        self.assertNotIn('token', json.dumps(summary))

    def test_public_timeout_identifies_unfinished_source_tests_without_test_fail_event(self):
        sources = {'internal/example/check_test.go':
                   'func TestPassed(t *testing.T) {}\nfunc TestPending(t *testing.T) {}\n'}
        package = 'cheesetrap/internal/example'
        events = [
            dict(Action='start', Package=package),
            dict(Action='run', Package=package, Test='TestPassed'),
            dict(Action='pass', Package=package, Test='TestPassed'),
            dict(Action='run', Package=package, Test='TestPending/runtime-secret'),
            dict(Action='run', Package=package, Test='TestUnknown/runtime-secret'),
            dict(Action='output', Package=package, Test='TestPending/runtime-secret',
                 Output='panic: test timed out after 3m0s\n'),
            dict(Action='fail', Package=package),
        ]
        summary = runner.public_diagnostics('\n'.join(map(json.dumps, events)), sources)
        self.assertEqual(summary['tests'], ['internal/example/check_test.go::TestPending'])
        self.assertEqual(summary['categories'], ['test_timeout'])
        self.assertNotIn('runtime-secret', json.dumps(summary))
        # A timeout-like log from a completed test is not evidence of a crash.
        events[-1:] = [dict(Action='pass', Package=package, Test='TestPending/runtime-secret'),
                       dict(Action='pass', Package=package)]
        self.assertEqual(runner.public_diagnostics('\n'.join(map(json.dumps, events)), sources)['tests'], [])

    def manifest(self):
        return {'schema': 1, 'requirements': [{'id': f'T{i:02}', 'tests': [{'suite': 'go', 'package': 'cheesetrap/example', 'name': f'Test{i}'}], 'linux': []} for i in range(1, 14)]}

    def test_checkout_provenance_observes_untracked_and_modified_inputs(self):
        self.assertTrue(callable(getattr(runner, 'checkout_is_clean', None)), 'checkout provenance recorder missing')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('.local/\n')
            (root / 'test.py').write_text('original')
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], cwd=root, check=True)
            self.assertTrue(runner.checkout_is_clean(root))
            (root / '.local').mkdir()
            (root / '.local/result.json').write_text('private result')
            self.assertTrue(runner.checkout_is_clean(root))
            (root / 'extra_test.py').write_text('untracked test')
            self.assertFalse(runner.checkout_is_clean(root))
            (root / 'extra_test.py').unlink()
            (root / 'test.py').write_text('modified test')
            self.assertFalse(runner.checkout_is_clean(root))
            (root / 'test.py').write_text('original')
            self.assertTrue(runner.checkout_is_clean(root))

    def test_manifest_requires_exact_traceability(self):
        runner.validate_manifest(self.manifest())
        for change in ('missing', 'duplicate', 'empty', 'unknown-suite'):
            manifest = self.manifest()
            if change == 'missing': manifest['requirements'].pop()
            if change == 'duplicate': manifest['requirements'][-1]['id'] = 'T01'
            if change == 'empty': manifest['requirements'][0]['tests'] = []
            if change == 'unknown-suite': manifest['requirements'][0]['tests'][0]['suite'] = 'mock-linux'
            with self.subTest(change=change), self.assertRaises(ValueError):
                runner.validate_manifest(manifest)

    def test_go_skip_fail_and_missing_never_pass(self):
        event = {'Package': 'cheesetrap/example', 'Test': 'Test1', 'Action': 'pass'}
        passed, errors = runner.go_results(json.dumps(event))
        self.assertIn(('cheesetrap/example', 'Test1'), passed)
        self.assertFalse(errors)
        for action in ('skip', 'fail'):
            event['Action'] = action
            passed, errors = runner.go_results(json.dumps(event))
            self.assertTrue(errors)
            self.assertNotIn(('cheesetrap/example', 'Test1'), passed)
        self.assertTrue(runner.go_results('no tests')[1])
        self.assertTrue(runner.go_results(json.dumps({'Action': 'fail', 'Package': 'broken'}))[1])

    def test_missing_test_fails_requirement(self):
        result = runner.evaluate(self.manifest(), {'go': set()}, {})
        self.assertTrue(all(row['status'] == 'FAIL' for row in result))

    def test_linux_cannot_be_replaced_by_unit_pass(self):
        manifest = self.manifest()
        manifest['requirements'][0]['linux'] = ['linux-public-boundary']
        passed = {'go': {('cheesetrap/example', f'Test{i}') for i in range(1, 14)}}
        result = runner.evaluate(manifest, passed, {})
        self.assertEqual(result[0]['status'], 'BLOCKED')
        self.assertEqual(result[1]['status'], 'PASS')

    def test_linux_evidence_requires_live_provenance_and_bound_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'probe.json').write_text('{"observed":true}')
            sha = hashlib.sha256((root / 'probe.json').read_bytes()).hexdigest()
            document = {'schema': 1, 'revision': 'abc', 'source_sha256': runner.production_sources(), 'environment': {'os': 'Linux', 'host': 'isolated-test', 'kernel': '6.8', 'docker': '29', 'actual_host': True}, 'checks': [{'id': 'linux-public-boundary', 'status': 'PASS', 'method': 'live-linux', 'command': 'paired probes', 'log': 'probe.json', 'sha256': sha, 'observed_at': '2026-10-21T00:00:00Z'}]}
            self.assertEqual(runner.linux_results(document, root, 'abc')['linux-public-boundary']['status'], 'PASS')
            for change in ('mock', 'desktop', 'missing-log', 'wrong-hash', 'old', 'invalid-status', 'unsupported-wrong-check', 'changed-source'):
                bad = copy.deepcopy(document)
                if change == 'changed-source': bad['source_sha256'] = {}
                if change == 'mock': bad['checks'][0]['method'] = 'mock'
                if change == 'desktop': bad['environment']['actual_host'] = False
                if change == 'missing-log': bad['checks'][0]['log'] = 'missing'
                if change == 'wrong-hash': bad['checks'][0]['sha256'] = '0'*64
                if change == 'old': bad['revision'] = 'old'
                if change == 'invalid-status': bad['checks'][0]['status'] = 'SKIP'
                if change == 'unsupported-wrong-check': bad['checks'][0]['status'] = 'UNSUPPORTED_ADDRESS_FAMILY'
                with self.subTest(change=change), self.assertRaises(ValueError):
                    runner.linux_results(bad, root, 'abc')
            unsupported = copy.deepcopy(document)
            unsupported['checks'][0].update(id='linux-ipv6', status='UNSUPPORTED_ADDRESS_FAMILY', reason='Public IPv6 unsupported; live disable state checked')
            self.assertEqual(runner.linux_results(unsupported, root, 'abc')['linux-ipv6']['status'], 'UNSUPPORTED_ADDRESS_FAMILY')

    def cleanup_harness(self, directory):
        harness = directory / 'harness.py'
        harness.write_text("""import os, signal, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
child = None
def cleanup(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if child is not None:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=2)
    (root / 'cleaned').write_text('yes')
    sys.exit(0)
signal.signal(signal.SIGTERM, cleanup)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
(root / 'child.pid').write_text(str(child.pid))
(root / 'ready').write_text('yes')
time.sleep(30)
""")
        return [sys.executable, str(harness), str(directory)]

    def assert_child_cleaned(self, directory):
        self.assertTrue((directory / 'cleaned').is_file(), 'harness cleanup was bypassed')
        pid = int((directory / 'child.pid').read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_timeout_allows_harness_to_clean_separate_child_group(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            command = self.cleanup_harness(directory)
            with self.assertRaises(runner.AcceptanceTimeout):
                runner.run_command(command, cwd=directory, env=os.environ.copy(), log=directory / 'run.log', timeout=1, cleanup_timeout=3)
            self.assert_child_cleaned(directory)

    def test_sigterm_forwards_then_stops_orchestration(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            command = self.cleanup_harness(directory)
            code = f"""import importlib.util, pathlib, os, sys
spec = importlib.util.spec_from_file_location('runner', {str(Path(runner.__file__).resolve())!r})
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
r.install_signal_handlers()
try:
    r.run_command({command!r}, cwd=pathlib.Path({temp!r}), env=os.environ.copy(), log=pathlib.Path({str(directory / 'run.log')!r}), timeout=30, cleanup_timeout=3)
    pathlib.Path({str(directory / 'continued')!r}).write_text('wrong')
except r.AcceptanceInterrupted:
    sys.exit(143)
"""
            outer = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not (directory / 'ready').exists() and outer.poll() is None and time.monotonic() < deadline: time.sleep(0.02)
                self.assertTrue((directory / 'ready').exists(), outer.communicate(timeout=1) if outer.poll() is not None else 'harness never ready')
                outer.send_signal(signal.SIGTERM)
                stdout, stderr = outer.communicate(timeout=6)
                self.assertEqual(outer.returncode, 143, stderr)
                self.assertFalse((directory / 'continued').exists())
                self.assert_child_cleaned(directory)
            finally:
                if outer.poll() is None: outer.kill(); outer.wait()

    def test_uncooperative_harness_is_killed_after_grace(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            pidfile = directory / 'pid'
            command = [sys.executable, '-c', f"import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(30)"]
            start = time.monotonic()
            with self.assertRaises(runner.AcceptanceTimeout):
                runner.run_command(command, cwd=directory, env=os.environ.copy(), log=directory / 'run.log', timeout=1, cleanup_timeout=0.2)
            self.assertLess(time.monotonic() - start, 4)
            with self.assertRaises(ProcessLookupError): os.kill(int(pidfile.read_text()), 0)

    def test_browser_names_and_skips(self):
        doc = {'suites': [{'suites': [{'specs': [{'file': 'review.spec.ts', 'title': 'review', 'tests': [{'expectedStatus': 'passed', 'status': 'expected', 'results': [{'status': 'passed'}]}]}]}]}]}
        self.assertEqual(runner.browser_results(doc), {('review.spec.ts', 'review')})
        doc['suites'][0]['suites'][0]['specs'][0]['tests'][0]['results'][0]['status'] = 'skipped'
        self.assertRaises(ValueError, runner.browser_results, doc)
        self.assertRaises(ValueError, runner.browser_results, {'suites': []})

    def test_automated_mode_never_claims_full_acceptance(self):
        rows = [{'status': 'BLOCKED'}]
        self.assertEqual(runner.run_status(rows, [], False), 'BLOCKED')
        self.assertEqual(runner.run_status(rows, [], True), 'PASS_AUTOMATED')
        self.assertEqual(runner.run_status(rows, [{'status': 'FAIL'}], True), 'FAIL')
        self.assertEqual(runner.run_status([{'status': 'FAIL'}], [], True), 'FAIL')

    def test_ui_skips_and_empty_are_failures(self):
        self.assertRaises(ValueError, runner.ui_results, {'success': True, 'testResults': []})
        result = {'success': True, 'testResults': [{'name': 'ui', 'status': 'passed', 'assertionResults': [{'title': 'escape HTML', 'fullName': 'escape HTML', 'status': 'passed'}]}]}
        self.assertEqual(runner.ui_results(result), {('ui', 'escape HTML')})
        result['testResults'][0]['assertionResults'][0]['status'] = 'pending'
        self.assertRaises(ValueError, runner.ui_results, result)

if __name__ == '__main__': unittest.main()
