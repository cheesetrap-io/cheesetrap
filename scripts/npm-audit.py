#!/usr/bin/env python3
"""Fail on locked npm advisories at low severity or above, except reviewed dev-only exceptions.

npm audit has no per-advisory ignore. This gate reads its JSON report and fails on
any advisory that is not listed in web/npm-audit-exceptions.json. An exception applies
only to its package, only while every locked copy is dev-only, and only until its
review_by date. An exception that npm no longer reports also fails, so it is removed.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
SEVERITIES = ('low', 'moderate', 'high', 'critical')
GHSA = re.compile(r'GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}')
EXCEPTION_KEYS = {'advisory', 'package', 'review_by', 'reason'}


def require(value, message):
    if not value:
        raise ValueError(message)


def load_exceptions(document):
    require(isinstance(document, dict) and set(document) == {'exceptions'},
            'exceptions file must contain only an "exceptions" list')
    entries = document['exceptions']
    require(isinstance(entries, list), 'exceptions must be a list')
    result = {}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == EXCEPTION_KEYS,
                f'each exception needs exactly {sorted(EXCEPTION_KEYS)}')
        advisory, package = entry['advisory'], entry['package']
        require(isinstance(advisory, str) and GHSA.fullmatch(advisory), f'invalid GHSA id: {advisory!r}')
        require(isinstance(package, str) and package, f'{advisory}: package is required')
        require(isinstance(entry['reason'], str) and entry['reason'].strip(), f'{advisory}: reason is required')
        try:
            review_by = dt.date.fromisoformat(entry['review_by'])
        except (TypeError, ValueError):
            raise ValueError(f'{advisory}: review_by must be YYYY-MM-DD') from None
        require((advisory, package) not in result, f'duplicate exception: {advisory} {package}')
        result[(advisory, package)] = review_by
    return result


def advisories(report):
    """Yield (package, GHSA id, severity, lock paths) for each advisory npm reported."""
    require(isinstance(report, dict) and 'error' not in report, f'npm audit failed: {report!r:.300}')
    require(report.get('auditReportVersion') == 2, 'unsupported npm audit report version')
    vulnerabilities = report.get('vulnerabilities')
    require(isinstance(vulnerabilities, dict), 'npm audit report has no vulnerabilities map')
    for package, vulnerability in sorted(vulnerabilities.items()):
        for via in vulnerability.get('via', []):
            if isinstance(via, str):
                continue  # Inherited from another reported package, which is checked itself.
            match = GHSA.search(str(via.get('url', '')))
            require(match, f'{package}: advisory without a GHSA id: {via!r:.300}')
            yield via.get('name', package), match.group(0), via.get('severity'), vulnerability.get('nodes', [])


def evaluate(report, lockfile, exception_document, today):
    exceptions = load_exceptions(exception_document)
    packages = lockfile.get('packages', {}) if isinstance(lockfile, dict) else {}
    problems, used = [], set()
    for package, advisory, severity, nodes in advisories(report):
        if severity not in SEVERITIES:
            continue
        key = (advisory, package)
        if key not in exceptions:
            problems.append(f'{package}: unreviewed {severity} advisory {advisory}')
            continue
        used.add(key)
        production = [node for node in nodes if packages.get(node, {}).get('dev') is not True]
        if not nodes or production:
            problems.append(f'{package}: {advisory} exception covers dev-only copies, '
                            f'but production or unknown copies are locked: {production or "none listed"}')
        if today > exceptions[key]:
            problems.append(f'{package}: {advisory} exception passed review_by {exceptions[key]}; '
                            're-review it or update the dependency')
    for advisory, package in sorted(set(exceptions) - used):
        problems.append(f'{package}: {advisory} is no longer reported; remove its exception')
    return problems


def run_npm_audit():
    completed = subprocess.run(
        ['npm', '--prefix', str(WEB), 'audit', '--package-lock-only', '--ignore-scripts', '--json'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, timeout=300, check=False)
    # npm exits 1 when it finds vulnerabilities, so the JSON body decides the result.
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ValueError(f'npm audit exited {completed.returncode} without JSON output') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--report', type=Path, help='saved `npm audit --json` output; default runs npm')
    parser.add_argument('--lockfile', type=Path, default=WEB / 'package-lock.json')
    parser.add_argument('--exceptions', type=Path, default=WEB / 'npm-audit-exceptions.json')
    parser.add_argument('--today', type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text()) if args.report else run_npm_audit()
        problems = evaluate(report, json.loads(args.lockfile.read_text()),
                            json.loads(args.exceptions.read_text()), args.today)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f'npm audit gate error: {error}', file=sys.stderr)
        return 1
    for problem in problems:
        print(f'FAIL {problem}', file=sys.stderr)
    if problems:
        return 1
    print('npm audit gate passed: no unreviewed advisories at low severity or above')
    return 0


if __name__ == '__main__':
    sys.exit(main())
