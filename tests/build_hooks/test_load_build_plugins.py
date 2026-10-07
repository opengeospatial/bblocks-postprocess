"""Tests for ogc.bblocks.build_hooks.plugin.load_build_plugins: plugins.build
config parsing, allowed_classes filtering, register_entries shape, and the
per-sandbox_dir memoization. Doesn't touch ensure_venv/pip install - construction
alone never calls those (they're lazy, only on first dispatch), so no subprocess
is spawned here.
"""
from ogc.bblocks.build_hooks import plugin as plugin_module
from ogc.bblocks.build_hooks.plugin import load_build_plugins


def _entries(*, classes, pip=None, url=None):
    entry = {'classes': classes}
    if pip is not None:
        entry['pip'] = pip
    if url is not None:
        entry['url'] = url
    return [entry]


def test_load_build_plugins_builds_one_plugin_per_class(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA', 'pkg.mod.ClassB']))
    plugins, entries = load_build_plugins(tmp_path)
    assert {p.class_path for p in plugins} == {'pkg.mod.ClassA', 'pkg.mod.ClassB'}
    assert entries == [{'classes': ['pkg.mod.ClassA', 'pkg.mod.ClassB']}]


def test_load_build_plugins_splits_module_and_class_name(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA']))
    plugins, _ = load_build_plugins(tmp_path)
    assert len(plugins) == 1
    assert plugins[0].module_path == 'pkg.mod'
    assert plugins[0].class_name == 'ClassA'


def test_load_build_plugins_skips_invalid_class_path(tmp_path, monkeypatch):
    # No dot -> not a valid 'module.ClassName' path.
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['NotAModulePath']))
    plugins, entries = load_build_plugins(tmp_path)
    assert plugins == []
    assert entries == []  # no output_classes -> no register entry emitted either


def test_load_build_plugins_respects_allowed_classes_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA', 'pkg.mod.ClassB']))
    plugins, entries = load_build_plugins(tmp_path, allowed_classes={'pkg.mod.ClassA'})
    assert {p.class_path for p in plugins} == {'pkg.mod.ClassA'}
    assert entries == [{'classes': ['pkg.mod.ClassA']}]


def test_load_build_plugins_none_allowed_classes_allows_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA']))
    plugins, _ = load_build_plugins(tmp_path, allowed_classes=None)
    assert len(plugins) == 1


def test_load_build_plugins_register_entry_derives_url_from_pip(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA'], pip=['my-package==1.0']))
    _, entries = load_build_plugins(tmp_path)
    assert entries == [{
        'classes': ['pkg.mod.ClassA'],
        'pip': ['my-package==1.0'],
        'urls': ['https://pypi.org/project/my-package'],
    }]


def test_load_build_plugins_register_entry_prefers_explicit_url(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA'], pip=['my-package==1.0'],
                                                 url='https://example.com/my-package'))
    _, entries = load_build_plugins(tmp_path)
    assert entries[0]['urls'] == ['https://example.com/my-package']


def test_load_build_plugins_memoizes_per_sandbox_dir(tmp_path, monkeypatch):
    calls = []
    def fake_read(section):
        calls.append(section)
        return _entries(classes=['pkg.mod.ClassA'])
    monkeypatch.setattr(plugin_module, 'read_plugin_entries', fake_read)

    result1 = load_build_plugins(tmp_path)
    result2 = load_build_plugins(tmp_path)
    assert result1 is result2  # same cached tuple object, not just equal
    assert len(calls) == 1  # config only read once


def test_load_build_plugins_does_not_memoize_across_different_sandbox_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries',
                        lambda section: _entries(classes=['pkg.mod.ClassA']))
    sandbox_a = tmp_path / 'a'
    sandbox_b = tmp_path / 'b'
    result_a = load_build_plugins(sandbox_a)
    result_b = load_build_plugins(sandbox_b)
    assert result_a is not result_b


# --- config / id ---------------------------------------------------------

import logging

import pytest


def _load(tmp_path, monkeypatch, entries, **kwargs):
    monkeypatch.setattr(plugin_module, 'read_plugin_entries', lambda section: entries)
    return load_build_plugins(tmp_path, **kwargs)


def test_config_and_id_are_carried_on_plugin_but_not_published(tmp_path, monkeypatch):
    plugins, entries = _load(tmp_path, monkeypatch, [
        {'id': 'strict', 'classes': ['pkg.mod.A'], 'config': {'foo': 'bar'}}])
    assert plugins[0].config == {'foo': 'bar'}
    assert plugins[0].plugin_id == 'strict'
    assert entries == [{'classes': ['pkg.mod.A']}]


def test_config_defaults_to_empty(tmp_path, monkeypatch):
    plugins, _ = _load(tmp_path, monkeypatch, [{'classes': ['pkg.mod.A']}])
    assert plugins[0].config == {}
    assert plugins[0].plugin_id is None


def test_config_applies_to_every_class_in_entry(tmp_path, monkeypatch):
    plugins, _ = _load(tmp_path, monkeypatch, [
        {'classes': ['pkg.mod.A', 'pkg.mod.B'], 'config': {'k': 1}}])
    assert [p.config for p in plugins] == [{'k': 1}, {'k': 1}]


def test_non_serializable_config_fails_early_without_venv_work(tmp_path, monkeypatch):
    import datetime
    monkeypatch.setattr(plugin_module.BuildPlugin, 'ensure_venv',
                        lambda *a, **k: pytest.fail('venv work before config validation'))
    with pytest.raises(ValueError, match='JSON-serializable'):
        _load(tmp_path, monkeypatch, [
            {'classes': ['pkg.mod.A'], 'config': {'when': datetime.date(2026, 1, 1)}}])


def test_non_string_config_keys_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='non-string key'):
        _load(tmp_path, monkeypatch, [{'classes': ['pkg.mod.A'], 'config': {'a': {1: 'x'}}}])


def test_nan_in_config_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='JSON-serializable'):
        _load(tmp_path, monkeypatch, [{'classes': ['pkg.mod.A'], 'config': {'a': float('nan')}}])


def test_non_mapping_config_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='must be a mapping'):
        _load(tmp_path, monkeypatch, [{'classes': ['pkg.mod.A'], 'config': ['x']}])


@pytest.mark.parametrize('bad_id', ['has space', 'a/b', '', 5])
def test_invalid_id_is_rejected(tmp_path, monkeypatch, bad_id):
    with pytest.raises(ValueError, match="invalid 'id'"):
        _load(tmp_path, monkeypatch, [{'id': bad_id, 'classes': ['pkg.mod.A']}])


def test_same_class_with_distinct_ids_is_allowed(tmp_path, monkeypatch):
    plugins, _ = _load(tmp_path, monkeypatch, [
        {'id': 'a', 'classes': ['pkg.mod.A'], 'config': {'n': 1}},
        {'id': 'b', 'classes': ['pkg.mod.A'], 'config': {'n': 2}}])
    assert [(p.plugin_id, p.config) for p in plugins] == [('a', {'n': 1}), ('b', {'n': 2})]


def test_duplicate_class_and_id_aborts(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='distinct'):
        _load(tmp_path, monkeypatch, [
            {'id': 'a', 'classes': ['pkg.mod.A']}, {'id': 'a', 'classes': ['pkg.mod.A']}])


def test_collision_detected_even_if_permissions_filter_it_out(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='distinct'):
        _load(tmp_path, monkeypatch, [
            {'id': 'a', 'classes': ['pkg.mod.A']}, {'id': 'a', 'classes': ['pkg.mod.A']}],
            allowed_classes=set())


def test_duplicate_idless_entries_warn_but_still_load(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        plugins, _ = _load(tmp_path, monkeypatch, [
            {'classes': ['pkg.mod.A']}, {'classes': ['pkg.mod.A']}])
    assert len(plugins) == 2
    assert any('without an' in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_same_class_different_ids_get_separate_processes_sharing_a_venv(tmp_path, monkeypatch):
    spawned = []

    class FakeProc:
        def __init__(self, python_bin, module_path, class_name, **kwargs):
            spawned.append(kwargs)

        def close(self):
            pass

    venvs = []
    monkeypatch.setattr(plugin_module, '_BuildHookProcess', FakeProc)
    monkeypatch.setattr(plugin_module, 'ensure_venv', lambda d: venvs.append(d))
    monkeypatch.setattr(plugin_module, '_process_cache', {})
    plugins, _ = _load(tmp_path, monkeypatch, [
        {'id': 'a', 'classes': ['pkg.mod.A'], 'config': {'n': 1}},
        {'id': 'b', 'classes': ['pkg.mod.A'], 'config': {'n': 2}}])
    procs = [p._process(tmp_path) for p in plugins]
    assert procs[0] is not procs[1]
    assert [k['plugin_id'] for k in spawned] == ['a', 'b']
    assert venvs[0] == venvs[1]


def test_build_hook_context_has_root_dir_and_every_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = plugin_module.build_hook_context(items_dir='items', base_url=None, register_file=None,
                                           steps=None, filter=None, fail_on_error=False)
    assert ctx['rootDir'] == str(tmp_path.resolve())
    assert set(ctx) == {'rootDir', 'itemsDir', 'baseUrl', 'registerFile', 'steps', 'filter', 'failOnError'}
