"""Integration test for the real _plugin_harness.py subprocess wire protocol
(Tier 2 of docs/pytest-testing-plan.md) - spawns the actual harness script
against tests/fixtures/hook_fixture_plugin.py, talking line-delimited JSON
over stdin/stdout exactly as ogc.bblocks.build_hooks.plugin._BuildHookProcess does.
No venv/pip install involved: the fixture plugin has no dependencies, so the
current interpreter is used directly as python_bin.

Directly regression-tests the bug fixed in commit 5cda6e0: _BuildHookProcess.send()
used to serialize requests with a bare json.dumps() and crashed (TypeError)
whenever an argument held a raw `set` - which bblock.metadata['shaclShapes'] can
be, when base_url is unset. See docs/build-lifecycle-hooks.md's "Post-merge fix".
"""
import sys
from pathlib import Path

import pytest

from ogc.bblocks.build_hooks.plugin import _BuildHookProcess

FIXTURES_DIR = Path(__file__).parent.parent / 'fixtures'


@pytest.fixture
def harness(monkeypatch):
    # _BuildHookProcess.send() spawns a real subprocess that imports the fixture
    # module by dotted path - make it importable via PYTHONPATH, inherited by the
    # child process since Popen() defaults to the parent's environment.
    monkeypatch.setenv('PYTHONPATH', str(FIXTURES_DIR))
    proc = _BuildHookProcess(Path(sys.executable), 'hook_fixture_plugin', 'FixtureBuildPlugin')
    yield proc
    proc.close()


def test_before_run_round_trip(harness):
    resp = harness.send('before_run', {'register': {'bblocks': []}, 'context': {'baseUrl': None}})
    assert resp['success'] is True
    assert resp['output'] is None


def test_after_register_returns_mutated_output(harness):
    resp = harness.send('after_register', {
        'register': {'bblocks': [], 'shapes': ['b', 'a']},
        'context': {},
    })
    assert resp['success'] is True
    assert resp['output']['seenShapes'] == ['a', 'b']


def test_send_survives_raw_set_in_register(harness):
    # The exact bug class from 5cda6e0 - send() must not raise client-side even
    # though a bare json.dumps() would TypeError on a raw set.
    register = {'bblocks': [{'shaclShapes': {'dep': {'a.ttl', 'b.ttl'}}}]}
    resp = harness.send('after_register', {'register': register, 'context': {}})
    assert resp['success'] is True


def test_before_bblock_reports_plugin_exception_without_raising(harness):
    # The harness itself never raises on a plugin method's own exception - it
    # reports success: False so the caller's fail_on_error rule decides what to do.
    resp = harness.send('before_bblock', {
        'stage': 'ANNOTATE',
        'bblock': {'identifier': 'boom'},
        'registerPath': None,
        'context': {},
    })
    assert resp['success'] is False
    assert 'deliberate failure for boom' in resp['error']


def test_before_bblock_captures_plugin_stdout_into_log(harness):
    resp = harness.send('before_bblock', {
        'stage': 'ANNOTATE',
        'bblock': {'identifier': 'ok-block'},
        'registerPath': None,
        'context': {},
    })
    assert resp['success'] is True
    assert 'before_bblock' in (resp['log'] or '')
    assert 'ok-block' in (resp['log'] or '')


def test_before_bblock_loads_register_from_registerpath(tmp_path, harness):
    import json
    register_file = tmp_path / 'register-annotate.json'
    register_file.write_text(json.dumps({'bblocks': ['x']}))
    resp = harness.send('before_bblock', {
        'stage': 'ANNOTATE',
        'bblock': {'identifier': 'ok-block'},
        'registerPath': str(register_file),
        'context': {},
    })
    assert resp['success'] is True
