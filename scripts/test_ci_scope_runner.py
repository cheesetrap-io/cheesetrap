"""Real Git histories prove that non-documentation changes cannot use the fast path."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('ci_scope', Path(__file__).with_name('ci-scope.py'))
SCOPE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCOPE)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.write('docs/guide.md', '# Guide\n')
        self.write('README.md', '# Fixture\n')
        self.write('go.mod', 'module fixture\n\ngo 1.27.4\n')
        self.write('value.go', 'package fixture\nfunc Value() int { return 7 }\n')
        self.write('value_test.go', 'package fixture\nimport "testing"\nfunc TestValue(t *testing.T) { if Value()!=7 {t.Fatal("wrong answer")} }\n')
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                        *args], cwd=self.root, text=True).strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def classify(self, event='pull_request', head=None):
        return SCOPE.scope(event, self.base, head or self.git('rev-parse', 'HEAD'), self.root)[0]

    def test_documentation_only_pr_uses_documentation_checks(self):
        self.write('docs/guide.md', '# Updated guide\n')
        self.write('docs/operations/new-guide.md', '# New guide\n')
        self.commit()
        self.assertEqual(self.classify(), 'docs')

    def test_hidden_runtime_change_selects_full_and_real_test_goes_red(self):
        self.write('docs/guide.md', '# Innocuous documentation\n')
        self.write('value.go', 'package fixture\nfunc Value() int { return 8 }\n')
        self.commit()
        self.assertEqual(self.classify(), 'full')
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR') if key in os.environ}
        env.update(GOTOOLCHAIN='local', GOENV='off', GOWORK='off', GOFLAGS='')
        result = subprocess.run(['go', 'test', './...', '-count=1'], cwd=self.root, env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('wrong answer', result.stdout, 'the intended assertion, not a build failure, must fail')

    def test_runtime_deletion_or_rename_to_markdown_stays_full(self):
        for rename in (False, True):
            with self.subTest(rename=rename):
                self.git('reset', '--hard', self.base)
                if rename: (self.root / 'value.go').rename(self.root / 'docs/renamed.md')
                else: (self.root / 'value.go').unlink()
                self.commit()
                self.assertEqual(self.classify(), 'full')

    def test_config_instructions_locks_and_unknown_paths_stay_full(self):
        for path in ('.github/workflows/ci.yml', 'AGENTS.md', 'docs/AGENTS.md', 'docs/data.json',
                     'web/package-lock.json', 'migrations/change.sql', 'other.md'):
            with self.subTest(path=path):
                self.git('reset', '--hard', self.base)
                self.write(path, 'changed\n')
                self.commit()
                self.assertEqual(self.classify(), 'full')

    def test_queue_push_and_manual_events_always_select_full(self):
        self.write('docs/guide.md', '# Updated guide\n')
        self.commit()
        for event in ('merge_group', 'push', 'workflow_dispatch', 'unknown'):
            with self.subTest(event=event): self.assertEqual(self.classify(event), 'full')

    def test_missing_or_empty_diff_stays_full(self):
        self.assertEqual(self.classify(), 'full')
        for base, head in (('', self.base), ('0' * 40, self.base), ('--all', self.base)):
            with self.subTest(base=base):
                self.assertEqual(SCOPE.scope('pull_request', base, head, self.root)[0], 'full')

    def test_shallow_checkout_can_fetch_missing_comparison_commit_once(self):
        self.write('docs/guide.md', '# Updated guide\n')
        head = self.commit()
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / 'clone'
            subprocess.run(['git', 'clone', '-q', '--no-local', '--depth=1', str(self.root), str(clone)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(SCOPE.scope('pull_request', self.base, head, clone)[0], 'full')
            self.assertEqual(SCOPE.scope('pull_request', self.base, head, clone, fetch_missing=True)[0], 'docs')

    def test_fetch_failure_and_unusual_markdown_name_stay_full(self):
        self.assertEqual(SCOPE.scope('pull_request', '0' * 40, self.base, self.root, fetch_missing=True)[0], 'full')
        self.write('docs/looks-like-docs\nvalue.go.md', '# Updated guide\n')
        self.commit()
        self.assertEqual(self.classify(), 'full')

    def test_symlink_and_executable_documentation_stay_full(self):
        self.write('docs/guide.md', '# Updated guide\n')
        (self.root / 'docs/guide.md').chmod(0o755)
        self.commit()
        self.assertEqual(self.classify(), 'full')
        self.git('reset', '--hard', self.base)
        (self.root / 'docs/guide.md').unlink()
        (self.root / 'docs/guide.md').symlink_to('../value.go')
        self.commit()
        self.assertEqual(self.classify(), 'full')


if __name__ == '__main__':
    unittest.main()
