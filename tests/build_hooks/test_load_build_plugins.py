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
