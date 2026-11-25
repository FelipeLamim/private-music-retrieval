"""Regression tests for publication of diagnostics, separate from live protocol data."""
import copy

from pmr.reporting import public_report


def test_report_preserves_measurements_and_original():
    raw = {'timings_ms': [0.0, 13.25], 'bytes': 8192, 'ok': True,
           'token': 'synthetic-test-value', 'tokens': 2, 'temp_clone_token': None}
    saved = copy.deepcopy(raw)
    clean = public_report(raw)
    assert raw == saved
    assert clean == {**raw, 'token': '<redacted>'}


def test_paths_are_portable_in_nested_reports():
    raw = {'argv': ['/home/example/project/src/pmr/experiments/runner.py',
                    '--out=/home/example/project/results/experiments/run',
                    '/home/example/.cache/private-profile',
                    r'C:\Users\example\project\tests\case.py']}
    clean = public_report(raw)
    assert clean['argv'] == ['src/pmr/experiments/runner.py',
                             '--out=results/experiments/run', '<local-path>', 'tests/case.py']
    assert public_report('/home/example/my project/results/run', root='/home/example/my project') == 'results/run'


def test_credentials_and_encoded_query_values_are_redacted():
    doc = {'url': 'http://127.0.0.1:8701/?t=fake%2Ftest%3D&mode=progressive',
           'log': 'X-PMR-Token: synthetic-test-value',
           'auth': 'Authorization: Bearer synthetic-test-value',
           'source': 'https://example:fake-password@example.invalid/path'}
    clean = public_report(doc)
    assert clean['url'] == 'http://127.0.0.1:8701/?t=<redacted>&mode=progressive'
    assert clean['log'] == 'X-PMR-Token: <redacted>'
    assert clean['auth'] == 'Authorization: <redacted>'
    assert clean['source'] == 'https://<redacted>@example.invalid/path'
    assert public_report(clean) == clean


def test_ephemeral_handles_redacted_only_on_export():
    path = '/v1/pir/queries/synthetic_test_handle_123/blocks?start=0&count=8'
    assert public_report(path, hide_handles=False) == path
    assert public_report(path) == '/v1/pir/queries/<redacted>/blocks?start=0&count=8'


def test_identity_fields_do_not_erase_system_characteristics():
    raw = {'hostname': 'test-host', 'username': 'example', 'cpu_model': 'test CPU',
           'logical_cpus': 4, 'listen': {'host': '127.0.0.1', 'port': 8700}}
    assert public_report(raw) == {**raw, 'hostname': '<redacted>', 'username': '<redacted>'}
