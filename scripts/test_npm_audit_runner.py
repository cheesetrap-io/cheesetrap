"""The npm audit gate fails on every advisory except reviewed, dev-only, unexpired exceptions."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('npm_audit', Path(__file__).with_name('npm-audit.py'))
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)

TODAY = dt.date(2026, 11, 18)
PICOMATCH = 'GHSA-fc5q-39xc-vjpf'


def advisory(package, ghsa, severity='high'):
    return {'source': 1, 'name': package, 'dependency': package, 'title': 'fixture',
            'url': f'https://github.com/advisories/{ghsa}', 'severity': severity, 'range': '*'}


def vulnerability(package, via, severity='high'):
    return {'name': package, 'severity': severity, 'isDirect': False, 'via': via,
            'effects': [], 'range': '*', 'nodes': [f'node_modules/{package}'], 'fixAvailable': False}


def report(*vulnerabilities):
    return {'auditReportVersion': 2,
            'vulnerabilities': {item['name']: item for item in vulnerabilities},
            'metadata': {'vulnerabilities': {}}}


def lock(**packages):
    entries = {'': {'name': 'web'}}
    for name, dev in packages.items():
        entry = {'version': '1.0.0'}
        if dev:
            entry['dev'] = True
        entries[f'node_modules/{name}'] = entry
    return {'lockfileVersion': 3, 'packages': entries}


def exception(ghsa=PICOMATCH, package='picomatch', review_by='2026-12-18'):
    return {'advisory': ghsa, 'package': package, 'review_by': review_by, 'reason': 'fixture review'}


class NpmAuditGateTests(unittest.TestCase):
    def evaluate(self, audit_report, lockfile, exceptions):
        return AUDIT.evaluate(audit_report, lockfile, {'exceptions': exceptions}, TODAY)

    def test_reviewed_dev_only_advisory_and_its_dependents_pass(self):
        audit_report = report(
            vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]),
            vulnerability('vite', ['picomatch']))
        self.assertEqual(self.evaluate(audit_report, lock(picomatch=True, vite=True), [exception()]), [])

    def test_unreviewed_advisory_fails(self):
        audit_report = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        problems = self.evaluate(audit_report, lock(picomatch=True), [])
        self.assertEqual(len(problems), 1)
        self.assertIn(PICOMATCH, problems[0])

    def test_new_advisory_on_excepted_package_still_fails(self):
        audit_report = report(vulnerability('picomatch', [
            advisory('picomatch', PICOMATCH), advisory('picomatch', 'GHSA-2222-3333-4444')]))
        problems = self.evaluate(audit_report, lock(picomatch=True), [exception()])
        self.assertEqual(len(problems), 1)
        self.assertIn('GHSA-2222-3333-4444', problems[0])

    def test_exception_does_not_cover_production_dependency(self):
        audit_report = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        problems = self.evaluate(audit_report, lock(picomatch=False), [exception()])
        self.assertEqual(len(problems), 1)
        self.assertIn('production', problems[0])

    def test_expired_exception_fails(self):
        audit_report = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        problems = self.evaluate(audit_report, lock(picomatch=True), [exception(review_by='2026-11-17')])
        self.assertTrue(any('review_by' in problem for problem in problems))

    def test_unused_exception_fails_so_it_is_removed(self):
        problems = self.evaluate(report(), lock(), [exception()])
        self.assertEqual(len(problems), 1)
        self.assertIn('no longer reported', problems[0])

    def test_info_severity_is_below_the_low_audit_level(self):
        audit_report = report(vulnerability(
            'picomatch', [advisory('picomatch', PICOMATCH, severity='info')], severity='info'))
        self.assertEqual(self.evaluate(audit_report, lock(picomatch=True), []), [])

    def test_npm_error_output_fails_closed(self):
        for broken in ({'error': {'code': 'ENOTFOUND', 'summary': 'network'}}, {}, []):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    self.evaluate(broken, lock(), [])

    def test_malformed_exception_is_rejected(self):
        audit_report = report()
        for bad in (exception(ghsa='CVE-2026-1'), exception(review_by='soon'),
                    {**exception(), 'reason': ''}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.evaluate(audit_report, lock(), [bad])

    def test_entries_the_gate_cannot_interpret_fail_closed(self):
        reviewed = vulnerability('picomatch', [advisory('picomatch', PICOMATCH)])
        cases = {
            'missing via': {**vulnerability('other', []), 'via': None},
            'empty via': vulnerability('other', []),
            'dangling reference': vulnerability('other', ['missing-package']),
            'unknown entry severity': vulnerability('other', ['picomatch'], severity='unknown'),
            'unknown advisory severity': vulnerability(
                'other', [advisory('other', 'GHSA-2222-3333-4444', severity='unknown')]),
            'missing nodes': {**vulnerability('other', ['picomatch']), 'nodes': []},
        }
        for name, broken in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    self.evaluate(report(reviewed, broken), lock(picomatch=True, other=True), [exception()])

    def test_reference_cycle_without_an_advisory_fails_closed(self):
        audit_report = report(vulnerability('a', ['b']), vulnerability('b', ['a']))
        with self.assertRaises(ValueError):
            self.evaluate(audit_report, lock(a=True, b=True), [])

    def test_unreported_production_copy_in_lockfile_still_blocks_exception(self):
        audit_report = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        lockfile = lock(picomatch=True)
        lockfile['packages']['node_modules/app/node_modules/picomatch'] = {'version': '4.0.7'}
        problems = self.evaluate(audit_report, lockfile, [exception()])
        self.assertEqual(len(problems), 1)
        self.assertIn('node_modules/app/node_modules/picomatch', problems[0])

    def test_nested_dev_copies_are_allowed(self):
        audit_report = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        lockfile = lock(picomatch=True)
        lockfile['packages']['node_modules/vite/node_modules/picomatch'] = {'version': '4.0.6', 'dev': True}
        self.assertEqual(self.evaluate(audit_report, lockfile, [exception()]), [])

    def test_similar_package_names_are_not_treated_as_copies(self):
        audit_report = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        lockfile = lock(picomatch=True)
        lockfile['packages']['node_modules/not-picomatch'] = {'version': '1.0.0'}
        self.assertEqual(self.evaluate(audit_report, lockfile, [exception()]), [])

    def test_npm_exit_code_must_match_its_report(self):
        vulnerable = report(vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))
        AUDIT.check_exit_code(1, vulnerable)
        AUDIT.check_exit_code(0, report())
        for code, audit_report in ((2, vulnerable), (0, vulnerable), (1, report()), (-9, report())):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    AUDIT.check_exit_code(code, audit_report)

    def test_npm_run_with_abnormal_exit_fails_even_with_valid_json(self):
        completed = subprocess.CompletedProcess([], 2, stdout=json.dumps(report(
            vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))))
        with mock.patch.object(AUDIT.subprocess, 'run', return_value=completed):
            with self.assertRaises(ValueError):
                AUDIT.run_npm_audit()

    def test_command_line_reads_saved_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'report.json').write_text(json.dumps(report(
                vulnerability('picomatch', [advisory('picomatch', PICOMATCH)]))))
            (root / 'package-lock.json').write_text(json.dumps(lock(picomatch=True)))
            (root / 'exceptions.json').write_text(json.dumps({'exceptions': [exception()]}))
            args = ['--report', str(root / 'report.json'), '--lockfile', str(root / 'package-lock.json'),
                    '--exceptions', str(root / 'exceptions.json'), '--today', TODAY.isoformat()]
            self.assertEqual(AUDIT.main(args), 0)
            (root / 'exceptions.json').write_text(json.dumps({'exceptions': []}))
            self.assertEqual(AUDIT.main(args), 1)


if __name__ == '__main__':
    unittest.main()
