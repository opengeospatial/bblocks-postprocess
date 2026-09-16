"""Unit tests for ogc.bblocks.build_hooks.plugin's dispatch logic (Tier 2 of
docs/pytest-testing-plan.md): seam ordering, after_register mutation
chaining, and the before_bblock/after_bblock two-tier failure rule from
docs/build-lifecycle-hooks.md's "Failure semantics".

Dispatch is exercised against fake plugin objects (a `class_path` attribute
plus a `dispatch(sandbox_dir, event, args)` method - the same surface
BuildPlugin.dispatch() exposes) rather than the real persistent-harness
subprocess, which tests/build_hooks/test_build_plugin_harness.py covers separately.
"""
import json
from pathlib import Path

import pytest

from ogc.bblocks.build_hooks.plugin import (
    Stage,
    dispatch_after_bblock,
    dispatch_after_register,
    dispatch_before_bblock,
    dispatch_before_run,
    dispatch_on_error,
    write_register_snapshot,
)

SANDBOX = Path('/nonexistent-sandbox')


class FakePlugin:
    """Records every dispatch() call and returns scripted responses in order."""

    def __init__(self, class_path, responses=None, fail=False):
        self.class_path = class_path
        self._responses = list(responses or [])
        self._fail = fail
        self.calls = []

    def dispatch(self, sandbox_dir, event, args):
        self.calls.append((event, args))
        if self._responses:
            return self._responses.pop(0)
        if self._fail:
            return {'success': False, 'error': 'boom', 'output': None}
        return {'success': True, 'error': None, 'output': None, 'log': None}


def test_checkpoint_dispatches_to_all_plugins_in_declaration_order():
    p1, p2 = FakePlugin('p1'), FakePlugin('p2')
    dispatch_before_run([p1, p2], SANDBOX, {'bblocks': []}, {})
    assert p1.calls and p2.calls
    assert p1.calls[0][0] == 'before_run'
    assert p2.calls[0][0] == 'before_run'


def test_checkpoint_raises_on_failure():
    p1 = FakePlugin('p1', fail=True)
    with pytest.raises(RuntimeError, match=r"before_run failed in build plugin 'p1'"):
        dispatch_before_run([p1], SANDBOX, {'bblocks': []}, {})


def test_after_register_chains_mutation_between_plugins():
    p1 = FakePlugin('p1', responses=[
        {'success': True, 'error': None, 'output': {'bblocks': ['a']}, 'log': None},
    ])
    p2 = FakePlugin('p2', responses=[
        {'success': True, 'error': None, 'output': None, 'log': None},  # observes only
    ])
    result = dispatch_after_register([p1, p2], SANDBOX, {'bblocks': []}, {})
    assert result == {'bblocks': ['a']}
    # p2 must see p1's mutation, not the original register
    assert p2.calls[0][1]['register'] == {'bblocks': ['a']}


def test_after_register_non_dict_output_leaves_register_unchanged():
    p1 = FakePlugin('p1', responses=[
        {'success': True, 'error': None, 'output': 'not-a-dict', 'log': None},
    ])
    result = dispatch_after_register([p1], SANDBOX, {'bblocks': []}, {})
    assert result == {'bblocks': []}


def test_after_register_raises_on_failure():
    p1 = FakePlugin('p1', fail=True)
    with pytest.raises(RuntimeError, match=r"after_register failed in build plugin 'p1'"):
        dispatch_after_register([p1], SANDBOX, {'bblocks': []}, {})


def test_bblock_event_fail_on_error_true_raises():
    p1 = FakePlugin('p1', fail=True)
    with pytest.raises(RuntimeError, match=r"before_bblock\(ANNOTATE\) failed for test:bb"):
        dispatch_before_bblock([p1], SANDBOX, Stage.ANNOTATE, {'identifier': 'test:bb'},
                               None, {}, fail_on_error=True)


def test_bblock_event_fail_on_error_false_logs_and_continues():
    p1 = FakePlugin('p1', fail=True)
    # Must not raise - the run continues with the bblock intact.
    dispatch_before_bblock([p1], SANDBOX, Stage.ANNOTATE, {'identifier': 'test:bb'},
                           None, {}, fail_on_error=False)
    assert p1.calls  # the plugin was still called


def test_bblock_event_payload_shape(tmp_path):
    p1 = FakePlugin('p1')
    register_path = tmp_path / 'register-finalize.json'
    dispatch_after_bblock([p1], SANDBOX, Stage.FINALIZE, {'identifier': 'x'},
                          register_path, {'light': False}, fail_on_error=True)
    event, args = p1.calls[0]
    assert event == 'after_bblock'
    assert args['stage'] == 'FINALIZE'
    assert args['registerPath'] == str(register_path)
    assert args['context'] == {'light': False}


def test_on_error_does_not_raise_when_plugin_fails():
    p1 = FakePlugin('p1', fail=True)
    # on_error's own dispatch failures are a recursion guard: logged, never raised,
    # so the caller's re-raise of the original error is never shadowed.
    dispatch_on_error([p1], SANDBOX, ValueError('original failure'), {'bblocks': []}, {},
                      phase='annotate')
    event, args = p1.calls[0]
    assert event == 'on_error'
    assert args['error']['type'] == 'ValueError'
    assert args['error']['message'] == 'original failure'
    assert args['error']['phase'] == 'annotate'
    assert 'ValueError' in args['error']['traceback']


def test_write_register_snapshot_survives_non_json_native_values(tmp_path):
    # Mirrors the bug class fixed in commit 5cda6e0: bblock.metadata['shaclShapes']
    # can be dict[str, set] when base_url is unset. write_register_snapshot already
    # goes through CustomJSONEncoder - this pins that behavior.
    register = {'bblocks': [{'shaclShapes': {'dep': {'a.ttl', 'b.ttl'}}}]}
    path = write_register_snapshot(tmp_path, Stage.ANNOTATE, register)
    assert path == tmp_path / 'hooks' / 'register-annotate.json'
    loaded = json.loads(path.read_text())
    assert sorted(loaded['bblocks'][0]['shaclShapes']['dep']) == ['a.ttl', 'b.ttl']
