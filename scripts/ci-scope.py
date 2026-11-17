#!/usr/bin/env python3
"""Choose PR checks conservatively; queue candidates always receive the full suite."""
import argparse
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def scope(event, base, head, root=ROOT, fetch_missing=False):
    if event != 'pull_request':
        return 'full', 'not_pull_request'
    if not all(re.fullmatch(r'[0-9a-f]{40}', value) for value in (base, head)):
        return 'full', 'missing_commit'

    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30).stdout

    try:
        missing = []
        for commit in (base, head):
            try: git('cat-file', '-e', commit + '^{commit}')
            except subprocess.CalledProcessError: missing.append(commit)
        if missing:
            if not fetch_missing: return 'full', 'missing_commit'
            # One shallow, blobless fetch; a failure falls back to all checks.
            git('fetch', '--no-tags', '--depth=1', '--filter=blob:none', 'origin', *missing)
        diff = git('diff', '--name-only', '--no-renames', '-z', base, head, '--')
        if not diff: return 'full', 'empty_diff'
        paths = [path.decode('utf-8') for path in diff.rstrip(b'\0').split(b'\0')]
        for path in paths:
            allowed = path in ('README.md', 'CONTRIBUTING.md') or re.fullmatch(
                r'docs/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md', path)
            if not allowed or Path(path).name == 'AGENTS.md':
                return 'full', 'non_documentation_change'
        seen = set()
        for commit in (base, head):
            entries = git('ls-tree', '-z', commit, '--', *paths)
            for entry in entries.rstrip(b'\0').split(b'\0') if entries else ():
                metadata, path = entry.split(b'\t', 1)
                if metadata.split()[:2] != [b'100644', b'blob']:
                    return 'full', 'non_regular_documentation'
                seen.add(path.decode('utf-8'))
        if seen != set(paths): return 'full', 'diff_unavailable'
        return 'docs', 'documentation_only'
    except (OSError, ValueError, subprocess.SubprocessError):
        # Never turn an unknown diff into a reduced successful check.
        return 'full', 'diff_unavailable'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', required=True)
    parser.add_argument('--base', default='')
    parser.add_argument('--head', default='')
    parser.add_argument('--fetch-missing', action='store_true')
    args = parser.parse_args()
    selected, reason = scope(args.event, args.base, args.head, fetch_missing=args.fetch_missing)
    print('CI_SCOPE ' + selected + ' ' + reason, flush=True)
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream: stream.write('scope=' + selected + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
