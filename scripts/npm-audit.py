#!/usr/bin/env python3
"""Fail on locked npm advisories at low severity or above, except reviewed dev-only exceptions.

npm audit has no per-advisory ignore. This gate reads its JSON report and fails on
any advisory that is not listed in web/npm-audit-exceptions.json. An exception applies
only to its package, only while every copy of that package in the lockfile is dev-only,
and only until its review_by date. An exception that npm no longer reports also fails,
so it is removed. Malformed reports, unknown severities, and unexpected npm exit codes
fail closed.
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
REPORTED = ('info',) + SEVERITIES  # info is below --audit-level=low and is skipped on purpose.
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
    """Yield (package, GHSA id, severity, lock paths) for each advisory at low severity or above.

    Every reported package must reach at least one advisory, directly or through string
    references to other reported packages, so a report this gate cannot interpret fails.
    """
    require(isinstance(report, dict) and 'error' not in report, f'npm audit failed: {report!r:.300}')
    require(report.get('auditReportVersion') == 2, 'unsupported npm audit report version')
    vulnerabilities = report.get('vulnerabilities')
    require(isinstance(vulnerabilities, dict), 'npm audit report has no vulnerabilities map')
    direct = {}
    for package, vulnerability in sorted(vulnerabilities.items()):
        require(isinstance(vulnerability, dict) and vulnerability.get('name') == package,
                f'{package}: malformed vulnerability entry')
        require(vulnerability.get('severity') in REPORTED, f'{package}: unknown severity {vulnerability.get("severity")!r}')
        via, nodes = vulnerability.get('via'), vulnerability.get('nodes')
        require(isinstance(via, list) and via, f'{package}: vulnerability without via entries')
        require(isinstance(nodes, list) and nodes and all(isinstance(node, str) for node in nodes),
                f'{package}: vulnerability without locked nodes')
        direct[package] = False
        for item in via:
            if isinstance(item, str):
                require(item in vulnerabilities, f'{package}: via references unreported package {item!r}')
                continue
            require(isinstance(item, dict), f'{package}: malformed via entry')
            match = GHSA.search(str(item.get('url', '')))
            require(match, f'{package}: advisory without a GHSA id: {item!r:.300}')
            severity = item.get('severity')
            require(severity in REPORTED, f'{package}: {match.group(0)} has unknown severity {severity!r}')
            require(isinstance(item.get('name'), str) and item['name'], f'{package}: {match.group(0)} has no package name')
            direct[package] = True
            if severity in SEVERITIES:
                yield item['name'], match.group(0), severity, nodes
    for package in direct:
        require(reaches_advisory(package, vulnerabilities, direct, set()),
                f'{package}: no advisory reachable through its via entries')


def reaches_advisory(package, vulnerabilities, direct, seen):
    if direct[package]:
        return True
    seen.add(package)
    return any(reaches_advisory(item, vulnerabilities, direct, seen)
               for item in vulnerabilities[package]['via'] if item not in seen)


def locked_copies(packages, package):
    """Every lockfile path that installs `package`, including nested copies."""
    suffix = 'node_modules/' + package
    return sorted(path for path in packages if path == suffix or path.endswith('/' + suffix))


def evaluate(report, lockfile, exception_document, today):
    exceptions = load_exceptions(exception_document)
    require(isinstance(lockfile, dict) and isinstance(lockfile.get('packages'), dict),
            'lockfile has no packages map')
    packages = lockfile['packages']
    problems, used = [], set()
    for package, advisory, severity, nodes in list(advisories(report)):
        key = (advisory, package)
        if key not in exceptions:
            problems.append(f'{package}: unreviewed {severity} advisory {advisory}')
            continue
        used.add(key)
        copies = sorted(set(locked_copies(packages, package)) | set(nodes))
        production = [path for path in copies if not isinstance(packages.get(path), dict)
                      or packages[path].get('dev') is not True]
        if production:
            problems.append(f'{package}: {advisory} exception covers dev-only copies, '
                            f'but production or unlocked copies exist: {production}')
        if today > exceptions[key]:
            problems.append(f'{package}: {advisory} exception passed review_by {exceptions[key]}; '
                            're-review it or update the dependency')
    for advisory, package in sorted(set(exceptions) - used):
        problems.append(f'{package}: {advisory} is no longer reported; remove its exception')
    return problems


def check_exit_code(returncode, report):
    """npm audit exits 1 exactly when it reports a vulnerability at low severity or above."""
    require(returncode in (0, 1), f'npm audit exited {returncode}')
    found = any(isinstance(item, dict) and item.get('severity') in SEVERITIES
                for item in report.get('vulnerabilities', {}).values())
    require(returncode == int(found), f'npm audit exit {returncode} disagrees with its report')


def run_npm_audit():
    completed = subprocess.run(
        ['npm', '--prefix', str(WEB), 'audit', '--package-lock-only', '--ignore-scripts', '--json'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, timeout=300, check=False)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise ValueError(f'npm audit exited {completed.returncode} without JSON output') from None
    require(isinstance(report, dict) and 'error' not in report, f'npm audit failed: {report!r:.300}')
    check_exit_code(completed.returncode, report)
    return report


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
