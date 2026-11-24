#!/usr/bin/env python3
"""Read-only first-attempt PR measurements; never dispatches or reruns CI."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import statistics
import subprocess
import sys
from urllib.parse import urlencode

REQUIRED = ('Secret scan', 'Dependency vulnerabilities', 'automated')
LEGACY_SHARDS = ('memory', 'jobs', 'retention', 'api', 'other', 'store')
WORK = {'acceptance': 'Run source and generated-code component',
        'browser': 'Run complete E2E and browser component',
        'testcontainers': 'Verify Testcontainers loopback ports and locked Ryuk image',
        'engines': 'Build pinned images'}
TERMINAL = {'success', 'failure', 'cancelled', 'timed_out', 'skipped', 'neutral', 'action_required', 'stale', 'startup_failure'}
CONFIG_FILES = ('.github/workflows/ci.yml', '.github/merge-queue.ruleset.json', '.nvmrc',
    'deploy/toolchain.lock.json', 'deploy/images.lock.json', 'deploy/Dockerfile',
    'sensors/images.lock', 'sensors/web/Dockerfile', 'go.mod', 'go.sum', 'web/package-lock.json',
    'web/playwright.config.ts', 'web/playwright.https.config.ts', 'tests/acceptance/coverage.json',
    'internal/reporting/memory_integration_test.go', 'scripts/test-acceptance.py',
    'scripts/test-integration.py', 'scripts/test-e2e.py', 'scripts/test-e2e.sh',
    'scripts/prepare-ci-web.sh', 'scripts/ci-scope.py', 'scripts/test-python.py',
    'scripts/release-check.py', 'scripts/release-check.sh', 'scripts/testpostgres/main.go')
SECURITY_POLICY_FILES = ('.gitleaks.toml', '.gitleaksignore', 'web/npm-audit-exceptions.json')


def require(value, message):
    if not value:
        raise ValueError(message)


def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(result.utcoffset() is not None, 'timestamp lacks timezone')
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError('missing or invalid timestamp') from None


def step_conclusion(job, name):
    rows = [s for s in job.get('steps', []) if s.get('name') == name]
    require(len(rows) == 1, 'missing or duplicate verification step')
    return rows[0].get('conclusion')


def associated(run, pr):
    links = run.get('pull_requests', [])
    if links:
        return any(p.get('number') == pr['number'] for p in links)
    head = pr.get('head', {})
    if not (head.get('ref') and head.get('repo', {}).get('full_name')):
        return False
    started = timestamp(run.get('created_at'))
    return (run.get('head_branch') == head['ref'] and
            run.get('head_repository', {}).get('full_name') == head['repo']['full_name'] and
            started >= timestamp(pr.get('created_at')) and
            (not pr.get('closed_at') or started <= timestamp(pr['closed_at'])))


def measure(repo, pr, run, jobs, checks, configuration, *, integration_shards=LEGACY_SHARDS):
    work = dict(WORK, **{'integration ('+name+')': 'Run required Go integration shard'
                        for name in integration_shards})
    require(pr.get('base', {}).get('repo', {}).get('full_name') == repo and
            run.get('repository', {}).get('full_name') == repo, 'repository mismatch')
    require(run.get('event') == 'pull_request' and run.get('run_attempt') == 1 and
            run.get('path') == '.github/workflows/ci.yml', 'not the first PR workflow attempt')
    require(run.get('status') == 'completed' and run.get('conclusion') in TERMINAL, 'run is incomplete')
    require(associated(run, pr), 'run is not associated with PR')
    head = run.get('head_sha', '')
    require(bool(re.fullmatch('[a-f0-9]{40}', head)), 'invalid head revision')
    rows = jobs.get('jobs', [])
    by_name = {j.get('name'): j for j in rows}
    require(jobs.get('total_count') == len(rows) == len(by_name) and
            set(by_name) == set(work) | set(REQUIRED), 'incomplete or unsupported job inventory')
    require(len(checks) == len(REQUIRED) and {c.get('name') for c in checks} == set(REQUIRED), 'missing or duplicate required context')
    created, started = timestamp(pr.get('created_at')), timestamp(run.get('created_at'))
    require(started >= created, 'run predates PR')
    completed = []
    for check in checks:
        job = by_name[check['name']]
        expected = f'https://github.com/{repo}/actions/runs/{run["id"]}/job/{job["id"]}'
        require(check.get('head_sha') == head and check.get('app', {}).get('id') == 15368 and
                check.get('status') == 'completed' and check.get('conclusion') in TERMINAL and
                check.get('details_url') == expected and
                job.get('check_run_url') == f'https://api.github.com/repos/{repo}/check-runs/{check.get("id")}' and
                job.get('conclusion') == check.get('conclusion'), 'required context attribution mismatch')
        completed.append(timestamp(check.get('completed_at')))
    require(all(t >= started for t in completed), 'context predates run')
    ready = max(completed)
    durations = {}
    for job in rows:
        require(job.get('status') == 'completed' and job.get('conclusion') in TERMINAL, 'job is incomplete')
        if job.get('started_at') is None and job['conclusion'] in ('cancelled', 'skipped'):
            seconds = None
        else:
            begin, end = timestamp(job.get('started_at')), timestamp(job.get('completed_at'))
            require(end >= begin >= started, 'invalid job interval')
            seconds = (end-begin).total_seconds()
        durations[job['name']] = dict(seconds=seconds, conclusion=job['conclusion'])
    docs = step_conclusion(by_name['acceptance'], 'Check documentation PR') == 'success'
    automated = next(c for c in checks if c['name'] == 'automated')
    if automated['conclusion'] == 'success':
        require(all(by_name[name]['conclusion'] == 'success' for name in work), 'automated verdict hides failed work')
    passed = all(c['conclusion'] == 'success' for c in checks)
    if run['conclusion'] == 'success':
        require(passed, 'successful run hides failed required context')
    if passed and not docs:
        require(all(step_conclusion(by_name[name], step) == 'success' for name, step in work.items()),
                'successful full result lacks required work')
    cancelled = run['conclusion'] == 'cancelled'
    return dict(pr=pr['number'], run=run['id'], head=head, configuration=configuration,
                attribution='native-pull-request' if run.get('pull_requests') else 'head-repository-branch-window-and-commit',
                url=f'https://github.com/{repo}/actions/runs/{run["id"]}',
                pr_created_at=pr['created_at'], run_created_at=run['created_at'],
                required_completed_at=ready.isoformat(), conclusion=run['conclusion'],
                eligible=not docs and not cancelled,
                excluded_reason='documentation-only' if docs else 'cancelled' if cancelled else None,
                pr_to_required_seconds=(ready-created).total_seconds(),
                pipeline_seconds=(ready-started).total_seconds(),
                trigger_delay_seconds=(started-created).total_seconds(),
                runner_job_seconds=sum(r['seconds'] for r in durations.values()) if
                    all(r['seconds'] is not None for r in durations.values()) else None,
                jobs=durations)


def summarize(records):
    require(len({r['pr'] for r in records}) == len(records) == len({r['run'] for r in records}), 'duplicate PR or run sample')
    groups = {}
    for configuration in sorted({r['configuration'] for r in records}):
        rows = [r for r in records if r['configuration'] == configuration and r['eligible']]
        if not rows:
            continue
        values = sorted(r['pr_to_required_seconds'] for r in rows)
        median, p95 = statistics.median(values), values[math.ceil(.95*len(values))-1]
        groups[configuration] = dict(sample_count=len(rows), median_seconds=median,
            p95_seconds_nearest_rank=p95, failures=sum(r['conclusion'] != 'success' for r in rows),
            runner_job_seconds=sum(r['runner_job_seconds'] for r in rows) if
                all(r['runner_job_seconds'] is not None for r in rows) else None,
            observed_targets_met=len(rows) >= 3 and median < 120 and p95 < 180,
            sample_limit='Empirical quantiles of the supplied first PR attempts; application code may differ. Small samples do not establish long-run p95.')
    return groups


def configuration_id(tree):
    require(tree.get('truncated') is False, 'configuration tree is incomplete')
    selected = {}
    tracked = set(CONFIG_FILES) | set(SECURITY_POLICY_FILES)
    for entry in tree.get('tree', []):
        path = entry.get('path', '')
        name = PurePosixPath(path).name
        procedure = path.startswith('scripts/') and name != 'ci-timings.py' and not name.startswith('test_') and not name.endswith('_test.go')
        if entry.get('type') == 'tree' or not (path in tracked or procedure):
            continue
        require(path not in selected and entry.get('type') == 'blob' and entry.get('mode') in ('100644', '100755') and
                bool(re.fullmatch('[a-f0-9]{40}', entry.get('sha', ''))), 'invalid configuration entry')
        selected[path] = entry['sha']
    require(set(CONFIG_FILES) <= selected.keys(), 'configuration inputs are missing')
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def workflow_shards(blob, expected_sha):
    require(blob.get('sha') == expected_sha and blob.get('encoding') == 'base64', 'workflow blob identity mismatch')
    try:
        raw = base64.b64decode(''.join(blob['content'].split()), validate=True)
        source = raw.decode('utf-8')
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        raise ValueError('invalid immutable workflow blob') from None
    require(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == expected_sha,
            'workflow content does not match immutable Git blob')
    lines = source.splitlines()
    starts = [i for i, line in enumerate(lines) if line == '  integration:']
    require(len(starts) == 1, 'unsupported integration workflow structure')
    block = []
    for line in lines[starts[0]+1:]:
        if line.strip() and line.startswith('  ') and not line.startswith('    '): break
        block.append(line)
    matrices, strategy, matrix = [], False, False
    for line in block:
        if not line.strip() or line.lstrip().startswith('#'): continue
        if re.match(r'^    \S', line):
            strategy, matrix = line == '    strategy:', False
        elif re.match(r'^      \S', line):
            matrix = strategy and line == '      matrix:'
        elif matrix and (match := re.fullmatch(r'        shard: \[([a-z, -]+)\]', line)):
            matrices.append(match)
    require(len(matrices) == 1, 'workflow must contain one supported literal shard matrix')
    shards = tuple(matrices[0].group(1).split(', '))
    require(len(shards) == len(set(shards)) and
            set(shards) in (set(LEGACY_SHARDS), set(LEGACY_SHARDS) | {'retention-recovery'}),
            'unsupported workflow shard inventory')
    return shards


def api_get(path):
    try:
        result = subprocess.run(['gh', 'api', '--hostname', 'github.com', '--method', 'GET', path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30, check=True)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('GitHub metadata read failed; no workflow was started or retried') from None


def collect(repo, number, run_id, read=None):
    read = api_get if read is None else read
    base = 'repos/'+repo
    pr = read(f'{base}/pulls/{number}')
    run = read(f'{base}/actions/runs/{run_id}/attempts/1')
    candidates, page, count = [], 1, None
    while count is None or len(candidates) < count:
        query = urlencode(dict(event='pull_request', created='>='+pr['created_at'][:10], per_page=100, page=page))
        data = read(f'{base}/actions/workflows/ci.yml/runs?{query}')
        count = data['total_count']
        require(type(count) is int and 0 <= count <= 1000 and bool(data['workflow_runs']), 'first-cohort history is incomplete or too large')
        candidates.extend(data['workflow_runs'])
        require(len(candidates) <= count, 'inconsistent first-cohort history')
        page += 1
    require(len({r['id'] for r in candidates}) == len(candidates), 'duplicate first-cohort history entries')
    cohorts = [r for r in candidates if r.get('event') == 'pull_request' and associated(r, pr)]
    require(bool(cohorts) and min(cohorts, key=lambda r: (timestamp(r['created_at']), r['id']))['id'] == run_id,
            'selected run is not the first associated PR cohort')
    closed_head = (pr.get('state') == 'closed' and pr.get('merged_at') is None and
                   pr.get('head', {}).get('sha') == run.get('head_sha') and
                   bool(re.fullmatch('[a-f0-9]{40}', run.get('head_sha', ''))))
    if not run.get('pull_requests') and not closed_head:
        links = read(f'{base}/commits/{run["head_sha"]}/pulls?per_page=100')
        require(any(p.get('number') == number and p.get('base', {}).get('repo', {}).get('full_name') == repo
                    for p in links), 'head commit lacks PR association')
    jobs = read(f'{base}/actions/runs/{run_id}/attempts/1/jobs?per_page=100')
    checks = []
    for job in jobs.get('jobs', []):
        if job.get('name') not in REQUIRED:
            continue
        url = job.get('check_run_url', '')
        require(bool(re.fullmatch(r'https://api\.github\.com/repos/'+re.escape(repo)+r'/check-runs/[0-9]+', url)),
                'invalid required-context URL')
        checks.append(read(url.removeprefix('https://api.github.com/')))
    tree_id = run.get('head_commit', {}).get('tree_id', '')
    require(bool(re.fullmatch('[a-f0-9]{40}', tree_id)), 'missing immutable source tree')
    tree = read(f'{base}/git/trees/{tree_id}?recursive=1')
    configuration = configuration_id(tree)
    workflow_sha = next(e['sha'] for e in tree['tree'] if e['path'] == '.github/workflows/ci.yml')
    shards = workflow_shards(read(f'{base}/git/blobs/{workflow_sha}'), workflow_sha)
    result = measure(repo, pr, run, jobs, checks, configuration, integration_shards=shards)
    if not run.get('pull_requests') and closed_head:
        result['attribution'] = 'head-repository-branch-window-and-pr-head'
    return result


def sample(value):
    if not re.fullmatch('[1-9][0-9]*:[1-9][0-9]*', value):
        raise argparse.ArgumentTypeError('sample must be PR_NUMBER:RUN_ID')
    return tuple(map(int, value.split(':')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='cheesetrap-io/cheesetrap')
    parser.add_argument('--sample', type=sample, action='append', required=True)
    args = parser.parse_args()
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', args.repo)), 'repository must be OWNER/NAME')
    require(len({p for p, _ in args.sample}) == len(args.sample) == len({r for _, r in args.sample}), 'duplicate PR or run sample')
    records = [collect(args.repo, number, run_id) for number, run_id in args.sample]
    print(json.dumps(dict(schema=1, repository=args.repo, records=records, by_configuration=summarize(records)), indent=2))


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        print(f'CI timing collection failed: {error}. No workflows were started or retried.', file=sys.stderr)
        raise SystemExit(1)
    except (KeyError, TypeError, AttributeError):
        print('CI timing collection failed: incomplete or unsupported metadata. No workflows were started or retried.', file=sys.stderr)
        raise SystemExit(1)
