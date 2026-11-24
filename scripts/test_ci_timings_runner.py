"""Synthetic GitHub metadata calibrates CI timing and failure reporting."""
import copy
import base64
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location('ci_timings', Path(__file__).with_name('ci-timings.py'))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
REPO = 'example/project'
SHA = '1' * 40
LEGACY_SHARDS = ('memory', 'jobs', 'retention', 'api', 'other', 'store')
RECOVERY_SHARDS = ('memory', 'jobs', 'retention-recovery', 'retention', 'api', 'other', 'store')


def workflow_blob(shards=LEGACY_SHARDS, source=None):
    source = source or ('jobs:\n  integration:\n    strategy:\n      matrix:\n        shard: [' + ', '.join(shards) + ']\n  automated:\n')
    raw = source.encode()
    sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    return dict(sha=sha, encoding='base64', content=base64.b64encode(raw).decode())


def at(seconds):
    return (dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


def fixture():
    steps = {'acceptance': 'Run source and generated-code component',
             'browser': 'Run complete E2E and browser component',
             'testcontainers': 'Verify Testcontainers loopback ports and locked Ryuk image',
             'engines': 'Build pinned images'}
    names = ['engines', 'testcontainers', 'browser', 'Secret scan', 'integration (api)',
             'integration (jobs)', 'integration (store)', 'integration (retention)',
             'integration (memory)', 'integration (other)', 'Dependency vulnerabilities', 'acceptance', 'automated']
    jobs, checks = [], []
    for number, (name, duration) in enumerate(zip(names, [65, 94, 106, 7, 82, 102, 47, 78, 96, 96, 28, 87, 3]), 1):
        start = 115 if name == 'automated' else 6
        job = dict(id=number, name=name, status='completed', conclusion='success',
                   started_at=at(start), completed_at=at(start + duration),
                   check_run_url=f'https://api.github.com/repos/{REPO}/check-runs/{100+number}', steps=[])
        if name.startswith('integration ('):
            job['steps'].append(dict(name='Run required Go integration shard', conclusion='success'))
        elif name in steps:
            job['steps'].append(dict(name=steps[name], conclusion='success'))
        if name == 'acceptance':
            job['steps'].append(dict(name='Check documentation PR', conclusion='skipped'))
        jobs.append(job)
        if name in ('Secret scan', 'Dependency vulnerabilities', 'automated'):
            checks.append(dict(id=100+number, name=name, head_sha=SHA, status='completed', conclusion='success',
                               app=dict(id=15368), completed_at=job['completed_at'],
                               details_url=f'https://github.com/{REPO}/actions/runs/42/job/{number}'))
    pr = dict(number=7, created_at=at(0), base=dict(repo=dict(full_name=REPO)),
              head=dict(ref='topic', repo=dict(full_name=REPO)), closed_at=at(200))
    run = dict(id=42, head_sha=SHA, event='pull_request', run_attempt=1, status='completed', conclusion='success',
               path='.github/workflows/ci.yml', created_at=at(4), repository=dict(full_name=REPO),
               pull_requests=[dict(number=7)], head_branch='topic', head_repository=dict(full_name=REPO))
    return pr, run, dict(total_count=13, jobs=jobs), checks


class CITimingTests(unittest.TestCase):
    def measure(self, data=None):
        return M.measure(REPO, *(data or fixture()), 'a' * 64)

    def test_includes_trigger_delay_and_uses_required_context_completion(self):
        pr, run, jobs, checks = fixture()
        run['updated_at'] = at(300)
        result = self.measure((pr, run, jobs, checks))
        self.assertEqual(result.get('pr_to_required_seconds'), 118)
        self.assertEqual(result.get('pipeline_seconds'), 114)
        self.assertEqual(result.get('trigger_delay_seconds'), 4)
        self.assertEqual(result.get('runner_job_seconds'), 891)
        self.assertIs(result.get('eligible'), True)

    def test_wrong_head_app_event_attempt_incomplete_or_false_green_is_rejected(self):
        for mode in ('head', 'app', 'manual', 'retry', 'pending', 'missing-check', 'duplicate-check',
                     'missing-job', 'duplicate-job', 'partial-page', 'missing-time', 'backwards-time',
                     'other-pr', 'other-run', 'skipped-work', 'false-green'):
            with self.subTest(mode=mode):
                pr, run, jobs, checks = fixture()
                if mode == 'head': checks[0]['head_sha'] = '2' * 40
                if mode == 'app': checks[0]['app']['id'] = 99
                if mode == 'manual': run['event'] = 'workflow_dispatch'
                if mode == 'retry': run['run_attempt'] = 2
                if mode == 'pending': run['status'] = 'in_progress'
                if mode == 'missing-check': checks.pop()
                if mode == 'duplicate-check': checks[-1] = copy.deepcopy(checks[0])
                if mode == 'missing-job': jobs['jobs'].pop()
                if mode == 'duplicate-job': jobs['jobs'][-1] = copy.deepcopy(jobs['jobs'][0])
                if mode == 'partial-page': jobs['total_count'] = 14
                if mode == 'missing-time': jobs['jobs'][0]['completed_at'] = None
                if mode == 'backwards-time': jobs['jobs'][0]['completed_at'] = at(1)
                if mode == 'other-pr': run['pull_requests'] = [dict(number=8)]
                if mode == 'other-run': checks[0]['details_url'] = checks[0]['details_url'].replace('/runs/42/', '/runs/43/')
                if mode == 'skipped-work': jobs['jobs'][0]['steps'][0]['conclusion'] = 'skipped'
                if mode == 'false-green': jobs['jobs'][0]['conclusion'] = 'failure'
                with self.assertRaises(ValueError): self.measure((pr, run, jobs, checks))

    def test_failed_first_attempt_is_retained(self):
        pr, run, jobs, checks = fixture()
        run['conclusion'] = 'failure'
        jobs['jobs'][0]['conclusion'] = 'failure'
        jobs['jobs'][0]['steps'][0]['conclusion'] = 'failure'
        jobs['jobs'][-1]['conclusion'] = 'failure'
        checks[-1]['conclusion'] = 'failure'
        result = self.measure((pr, run, jobs, checks))
        self.assertIs(result.get('eligible'), True)
        self.assertEqual(result.get('conclusion'), 'failure')
        self.assertEqual(result.get('pr_to_required_seconds'), 118)

    def test_docs_and_cancellations_keep_cost_but_do_not_become_normal_samples(self):
        for mode in ('docs', 'cancelled'):
            with self.subTest(mode=mode):
                pr, run, jobs, checks = fixture()
                if mode == 'docs':
                    for job in jobs['jobs']:
                        for step in job['steps']: step['conclusion'] = 'success' if step['name'] == 'Check documentation PR' else 'skipped'
                else:
                    run['conclusion'] = 'cancelled'
                    for job in jobs['jobs']: job['conclusion'] = 'cancelled'
                    for check in checks: check['conclusion'] = 'cancelled'
                result = self.measure((pr, run, jobs, checks))
                self.assertIs(result.get('eligible'), False)
                self.assertEqual(result.get('runner_job_seconds'), 891)

    def test_configurations_are_separate_and_failed_results_are_in_quantiles(self):
        records = [dict(pr=i+1, run=i+10, configuration='a' * 64 if i<3 else 'b' * 64,
                        eligible=True, conclusion='failure' if i==0 else 'success',
                        pr_to_required_seconds=seconds, runner_job_seconds=100)
                   for i, seconds in enumerate([140, 118, 119, 300])]
        groups = M.summarize(records)
        current = groups.get('a' * 64, {})
        self.assertEqual(current.get('sample_count'), 3)
        self.assertEqual(current.get('median_seconds'), 119)
        self.assertEqual(current.get('p95_seconds_nearest_rank'), 140)
        self.assertEqual(current.get('failures'), 1)
        self.assertEqual(groups.get('b' * 64, {}).get('sample_count'), 1)

    def test_duplicate_pr_or_run_cannot_inflate_the_sample(self):
        record = dict(pr=1, run=10, configuration='a' * 64, eligible=True,
                      conclusion='success', pr_to_required_seconds=118, runner_job_seconds=100)
        for other in (dict(record), dict(record, pr=2), dict(record, run=11)):
            with self.subTest(other=other), self.assertRaises(ValueError): M.summarize([record, other])

    def test_configuration_changes_are_separate_from_application_and_test_changes(self):
        tree = dict(truncated=False, tree=[dict(path=p, type='blob', mode='100644', sha=SHA) for p in M.CONFIG_FILES])
        baseline = M.configuration_id(tree)
        self.assertRegex(baseline, '^[a-f0-9]{64}$')
        reordered = dict(tree, tree=list(reversed(tree['tree'])))
        self.assertEqual(M.configuration_id(reordered), baseline)
        unrelated = copy.deepcopy(tree)
        for path in ('scripts/ci-timings.py', 'scripts/test_ci_timings_runner.py', 'internal/reporting/draft.go'):
            unrelated['tree'].append(dict(path=path, type='blob', mode='100644', sha='2' * 40))
        self.assertEqual(M.configuration_id(unrelated), baseline)
        changed = copy.deepcopy(tree)
        changed['tree'][0]['sha'] = '2' * 40
        self.assertNotEqual(M.configuration_id(changed), baseline)
        for bad in (dict(tree, truncated=True), dict(tree, tree=tree['tree'][1:]),
                    dict(tree, tree=tree['tree'] + tree['tree'][:1])):
            with self.subTest(bad=bad), self.assertRaises(ValueError): M.configuration_id(bad)

    def test_security_policy_changes_split_timing_groups_including_historical_absence(self):
        tree = dict(truncated=False, tree=[dict(path=p, type='blob', mode='100644', sha=SHA) for p in M.CONFIG_FILES])
        historical = M.configuration_id(tree)
        for path in ('web/npm-audit-exceptions.json', '.gitleaks.toml', '.gitleaksignore'):
            with self.subTest(path=path):
                policy = dict(path=path, type='blob', mode='100644', sha='2' * 40)
                previous = M.configuration_id(dict(tree, tree=[*tree['tree'], policy]))
                updated = M.configuration_id(dict(tree, tree=[*tree['tree'], dict(policy, sha='3' * 40)]))
                self.assertEqual(len({historical, previous, updated}), 3)

    def test_workflow_inventory_is_bound_to_immutable_blob_and_supported_literal_matrix(self):
        parse = getattr(M, 'workflow_shards', None)
        self.assertTrue(callable(parse), 'native inventory must come from the exact run workflow blob')
        for shards in (LEGACY_SHARDS, RECOVERY_SHARDS):
            blob = workflow_blob(shards)
            self.assertEqual(parse(blob, blob['sha']), shards)
        good = workflow_blob()
        malformed = [dict(good, sha='2' * 40), dict(good, encoding='utf-8'), dict(good, content='bad%'),
                     dict(good, content=workflow_blob(RECOVERY_SHARDS)['content']),
                     workflow_blob(('memory', 'jobs', 'other')),
                     workflow_blob(LEGACY_SHARDS + ('jobs',)),
                     workflow_blob(source='jobs:\n  integration:\n    strategy:\n      matrix: ${{ fromJSON(inputs.matrix) }}\n'),
                     workflow_blob(source='jobs:\n  integration:\n    env:\n      DESCRIPTION: |\n        shard: [memory, jobs, retention, api, other, store]\n    strategy:\n      matrix: ${{ fromJSON(inputs.matrix) }}\n'),
                     workflow_blob(source='jobs:\n  integration:\n    strategy:\n      DESCRIPTION: |\n        shard: [memory, jobs, retention, api, other, store]\n'),
                     workflow_blob(source='jobs:\n  integration:\n        shard: [memory]\n        shard: [jobs]\n')]
        for blob in malformed:
            with self.subTest(blob=blob), self.assertRaises(ValueError): parse(blob, good['sha'] if blob in malformed[:4] else blob['sha'])

    def test_new_workflow_cannot_pass_without_its_required_recovery_job(self):
        pr, run, jobs, checks = fixture()
        try:
            M.measure(REPO, pr, run, jobs, checks, 'a' * 64, integration_shards=RECOVERY_SHARDS)
        except ValueError:
            pass
        except TypeError:
            self.fail('measure must validate the exact historical workflow shard inventory')
        else:
            self.fail('a seven-shard workflow cannot accept the old thirteen-job result')
        extra = copy.deepcopy(jobs['jobs'][4])
        extra.update(id=77, name='integration (retention-recovery)', started_at=at(6), completed_at=at(16))
        jobs['jobs'].append(extra); jobs['total_count'] = 14
        result = M.measure(REPO, pr, run, jobs, checks, 'a' * 64, integration_shards=RECOVERY_SHARDS)
        self.assertEqual(result['runner_job_seconds'], 901)
        with self.assertRaises(ValueError): self.measure((pr, run, jobs, checks))
        extra['conclusion'] = 'skipped'
        with self.assertRaises(ValueError):
            M.measure(REPO, pr, run, jobs, checks, 'a' * 64, integration_shards=RECOVERY_SHARDS)

    def metadata_reader(self, *, earlier=False, rerun=False, recovery=False, omit_recovery=False):
        pr, run, jobs, checks = fixture()
        run['head_commit'] = dict(tree_id='2' * 40)
        listing = copy.deepcopy(run)
        if rerun:
            listing.update(run_attempt=2, conclusion='success')
            run['conclusion'] = 'failure'
            jobs['jobs'][0]['conclusion'] = 'failure'
            jobs['jobs'][0]['steps'][0]['conclusion'] = 'failure'
            jobs['jobs'][-1]['conclusion'] = 'failure'
            checks[-1]['conclusion'] = 'failure'
        history = [listing]
        if earlier:
            history.append(dict(listing, id=41, created_at=at(2)))
        tree = dict(truncated=False, tree=[dict(path=p, type='blob', mode='100644', sha=SHA) for p in M.CONFIG_FILES])
        blob = workflow_blob(RECOVERY_SHARDS if recovery else LEGACY_SHARDS)
        next(e for e in tree['tree'] if e['path'] == '.github/workflows/ci.yml')['sha'] = blob['sha']
        if recovery and not omit_recovery:
            extra = copy.deepcopy(jobs['jobs'][4])
            extra.update(id=77, name='integration (retention-recovery)', started_at=at(6), completed_at=at(16),
                         check_run_url=f'https://api.github.com/repos/{REPO}/check-runs/177')
            jobs['jobs'].append(extra); jobs['total_count'] = 14
        routes = {f'repos/{REPO}/pulls/7': pr,
                  f'repos/{REPO}/actions/runs/42/attempts/1': run,
                  f'repos/{REPO}/actions/runs/42/attempts/1/jobs?per_page=100': jobs,
                  f'repos/{REPO}/git/trees/{"2"*40}?recursive=1': tree,
                  f'repos/{REPO}/git/blobs/{blob["sha"]}': blob,
                  f'repos/{REPO}/commits/{SHA}/pulls?per_page=100': [pr],
                  **{f'repos/{REPO}/check-runs/{c["id"]}': c for c in checks}}
        calls = []
        def read(path):
            calls.append(path)
            if path.startswith(f'repos/{REPO}/actions/workflows/ci.yml/runs?'):
                return dict(total_count=len(history), workflow_runs=copy.deepcopy(history))
            self.assertIn(path, routes)
            return copy.deepcopy(routes[path])
        return read, calls

    def test_collection_rejects_a_missing_new_shard_even_when_old_contexts_are_green(self):
        read, calls = self.metadata_reader(recovery=True)
        result = M.collect(REPO, 7, 42, read=read)
        self.assertEqual(len(result['jobs']), 14)
        self.assertTrue(any('/git/blobs/' in path for path in calls))
        read, _ = self.metadata_reader(recovery=True, omit_recovery=True)
        with self.assertRaisesRegex(ValueError, 'job inventory'): M.collect(REPO, 7, 42, read=read)

    def test_collection_uses_attempt_one_and_keeps_its_failure_after_a_rerun(self):
        read, calls = self.metadata_reader(rerun=True)
        result = M.collect(REPO, 7, 42, read=read)
        self.assertEqual(result['conclusion'], 'failure')
        self.assertEqual(result['pr_to_required_seconds'], 118)
        self.assertIn(f'repos/{REPO}/actions/runs/42/attempts/1', calls)
        self.assertNotIn(f'repos/{REPO}/actions/runs/42', calls)

    def test_later_head_cohort_cannot_replace_the_first_result(self):
        read, _ = self.metadata_reader(earlier=True)
        with self.assertRaisesRegex(ValueError, 'not the first'): M.collect(REPO, 7, 42, read=read)

    def test_merged_pr_without_native_links_requires_branch_window_and_commit_proof(self):
        for mode in ('valid', 'repository', 'branch', 'before-open', 'after-close', 'commit', 'earlier'):
            with self.subTest(mode=mode):
                read, _ = self.metadata_reader(earlier=mode == 'earlier')
                def omitted(path):
                    data = read(path)
                    rows = data.get('workflow_runs', [data]) if isinstance(data, dict) else []
                    for row in rows:
                        if 'run_attempt' not in row: continue
                        row['pull_requests'] = []
                        if mode == 'repository': row['head_repository']['full_name'] = 'other/project'
                        if mode == 'branch': row['head_branch'] = 'other'
                        if mode == 'before-open': row['created_at'] = at(-1)
                        if mode == 'after-close': row['created_at'] = at(201)
                    if mode == 'commit' and '/commits/' in path: return []
                    return data
                if mode == 'valid':
                    try:
                        result = M.collect(REPO, 7, 42, read=omitted)
                    except ValueError:
                        self.fail('verified merged PR must remain measurable when native PR links are empty')
                    self.assertEqual(result['pr_to_required_seconds'], 118)
                    self.assertEqual(result['attribution'], 'head-repository-branch-window-and-commit')
                else:
                    with self.assertRaises(ValueError): M.collect(REPO, 7, 42, read=omitted)

    def test_closed_unmerged_pr_keeps_its_first_failure_with_exact_head_proof(self):
        for mode in ('valid', 'head', 'repository', 'branch', 'after-close', 'later-cohort', 'required-head'):
            with self.subTest(mode=mode):
                read, _ = self.metadata_reader(rerun=True, earlier=mode == 'later-cohort')
                def closed(path):
                    data = read(path)
                    if '/commits/' in path: return []
                    if path.endswith('/pulls/7'):
                        data.update(state='closed', merged_at=None)
                        data['head']['sha'] = '2' * 40 if mode == 'head' else SHA
                    rows = data.get('workflow_runs', [data]) if isinstance(data, dict) else []
                    for row in rows:
                        if 'run_attempt' not in row: continue
                        row['pull_requests'] = []
                        if mode == 'repository': row['head_repository']['full_name'] = 'other/project'
                        if mode == 'branch': row['head_branch'] = 'other'
                        if mode == 'after-close': row['created_at'] = at(201)
                    if mode == 'required-head' and '/check-runs/' in path: data['head_sha'] = '2' * 40
                    return data
                if mode == 'valid':
                    try:
                        result = M.collect(REPO, 7, 42, read=closed)
                    except ValueError:
                        self.fail('a closed unmerged first failure with exact PR head proof must remain measurable')
                    self.assertEqual(result['conclusion'], 'failure')
                    self.assertEqual(result['pr_to_required_seconds'], 118)
                    self.assertIs(result['eligible'], True)
                    self.assertEqual(result['attribution'], 'head-repository-branch-window-and-pr-head')
                else:
                    with self.assertRaises(ValueError): M.collect(REPO, 7, 42, read=closed)

    def test_github_client_only_issues_get_requests_and_hides_raw_api_errors(self):
        with mock.patch.object(M.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps({'ok': True}))) as run:
            self.assertEqual(M.api_get('repos/example/project/actions/runs/42'), {'ok': True})
            self.assertEqual(run.call_args.args[0][:7], ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', 'repos/example/project/actions/runs/42'])
            self.assertEqual(run.call_args.kwargs['stderr'], M.subprocess.DEVNULL)
        with mock.patch.object(M.subprocess, 'run', side_effect=M.subprocess.CalledProcessError(1, ['private-runtime-canary'])):
            with self.assertRaisesRegex(ValueError, '^GitHub metadata read failed; no workflow was started or retried$'):
                M.api_get('repos/example/project/actions/runs/42')
