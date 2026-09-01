"""Tests for the build-plugin-specific additions to ogc.bblocks.permissions:
check_build_plugin_permissions() and _check_plugin_permissions()'s key='classes'
path. The generic, already-on-master permission-decision logic (ask_yes_no, the
cache round-trip, the key='modules' path used by transform/validator plugins) is
covered by tests/test_permissions.py once that lands here too - this file covers
only what build-plugins adds on top.
"""
import builtins
import json

from ogc.bblocks import permissions as permissions_module
from ogc.bblocks.permissions import _check_plugin_permissions, check_build_plugin_permissions


def test_check_plugin_permissions_keys_on_classes_not_modules(monkeypatch):
    monkeypatch.setattr(builtins, 'input', lambda _: 'y')
    allowed, dirty = _check_plugin_permissions(
        [{'classes': ['pkg.mod.ClassA', 'pkg.mod.ClassB']}], 'build-plugins', {}, 'Build',
        key='classes',
    )
    assert allowed == {'pkg.mod.ClassA', 'pkg.mod.ClassB'}
    assert dirty is True


def test_check_plugin_permissions_classes_approval_is_per_class(monkeypatch):
    # Approving one class must not silently approve a sibling class in the same
    # module - the whole point of the stricter class-level keying (vs. modules').
    cache = {'build-plugins': {'pkg.mod.ClassA': ''}}
    monkeypatch.setattr(builtins, 'input', lambda _: 'n')  # deny anything actually prompted
    allowed, dirty = _check_plugin_permissions(
        [{'classes': ['pkg.mod.ClassA', 'pkg.mod.ClassB']}], 'build-plugins', cache, 'Build',
        key='classes',
    )
    assert allowed == {'pkg.mod.ClassA'}  # cached, no prompt needed
    assert 'pkg.mod.ClassB' not in allowed  # prompted (denied) independently


def test_check_build_plugin_permissions_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions_module, 'read_plugin_entries',
                        lambda section: [{'classes': ['pkg.mod.ClassA']}] if section == 'build' else [])
    monkeypatch.setattr(builtins, 'input', lambda _: 'y')
    allowed = check_build_plugin_permissions(tmp_path)
    assert allowed == {'pkg.mod.ClassA'}
    saved = json.loads((tmp_path / 'permissions.json').read_text())
    assert saved['build-plugins']['pkg.mod.ClassA'] == ''


def test_check_build_plugin_permissions_does_not_scan_risky_transforms(tmp_path, monkeypatch):
    # Unlike check_permissions(), this has no items_dir to scan - _scan_risky_transforms
    # is unrelated to build plugins and must not be invoked here.
    def fail(*a, **kw):
        raise AssertionError('_scan_risky_transforms should not be called')
    monkeypatch.setattr(permissions_module, '_scan_risky_transforms', fail)
    monkeypatch.setattr(permissions_module, 'read_plugin_entries', lambda section: [])
    check_build_plugin_permissions(tmp_path)


def test_check_build_plugin_permissions_reuses_cache_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions_module, 'read_plugin_entries',
                        lambda section: [{'classes': ['pkg.mod.ClassA']}])
    calls = []
    monkeypatch.setattr(builtins, 'input', lambda prompt: (calls.append(prompt), 'y')[1])

    check_build_plugin_permissions(tmp_path)
    assert len(calls) == 1

    check_build_plugin_permissions(tmp_path)  # reads the persisted cache from disk
    assert len(calls) == 1  # no new prompt


def test_check_build_plugin_permissions_denial_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions_module, 'read_plugin_entries',
                        lambda section: [{'classes': ['pkg.mod.ClassA']}])
    monkeypatch.setattr(builtins, 'input', lambda _: 'n')
    allowed = check_build_plugin_permissions(tmp_path)
    assert allowed == set()
    assert not (tmp_path / 'permissions.json').exists()
