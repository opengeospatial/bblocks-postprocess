"""Tests for ogc.bblocks.permissions's pure/near-pure permission-decision logic
(Tier 1, docs/pytest-testing-plan.md): the on-disk cache round-trip, ask_yes_no's
prompt handling (including the closed-stdin/EOF path), and
_check_plugin_permissions's cache-hit / prompt / stale-version behavior.

check_permissions() itself (the _scan_risky_transforms + read_plugin_entries
orchestration) isn't covered yet.
"""
import builtins

from ogc.bblocks.permissions import (
    _check_plugin_permissions,
    _load_cache,
    _plugin_version_key,
    _save_cache,
    ask_yes_no,
)


def test_plugin_version_key_sorts_and_joins():
    assert _plugin_version_key({'pip': ['b==2', 'a==1']}) == 'a==1,b==2'


def test_plugin_version_key_accepts_bare_string():
    assert _plugin_version_key({'pip': 'a==1'}) == 'a==1'


def test_plugin_version_key_defaults_to_empty_string():
    assert _plugin_version_key({}) == ''


def test_load_cache_missing_file_returns_empty_dict(tmp_path):
    assert _load_cache(tmp_path) == {}


def test_load_cache_corrupt_file_returns_empty_dict(tmp_path):
    (tmp_path / 'permissions.json').write_text('{not valid json')
    assert _load_cache(tmp_path) == {}


def test_save_and_load_cache_round_trip(tmp_path):
    _save_cache(tmp_path, {'plugins': {'mod.Foo': 'a==1'}})
    assert _load_cache(tmp_path) == {'plugins': {'mod.Foo': 'a==1'}}


def test_ask_yes_no_accepts_y_variants(monkeypatch):
    for answer in ('y', 'Y', 'yes', 'YES'):
        monkeypatch.setattr(builtins, 'input', lambda _: answer)
        assert ask_yes_no('Allow?') is True


def test_ask_yes_no_accepts_n_variants_and_empty(monkeypatch):
    for answer in ('n', 'N', 'no', ''):
        monkeypatch.setattr(builtins, 'input', lambda _: answer)
        assert ask_yes_no('Allow?') is False


def test_ask_yes_no_reprompts_on_garbage_then_accepts(monkeypatch, capsys):
    answers = iter(['maybe', 'y'])
    monkeypatch.setattr(builtins, 'input', lambda _: next(answers))
    assert ask_yes_no('Allow?') is True
    assert 'Please answer y or n' in capsys.readouterr().out


def test_ask_yes_no_denies_on_closed_stdin(monkeypatch, caplog):
    def raise_eof(_):
        raise EOFError()
    monkeypatch.setattr(builtins, 'input', raise_eof)
    with caplog.at_level('WARNING'):
        result = ask_yes_no('Allow?')
    assert result is False
    assert 'No interactive input' in caplog.text


def test_check_plugin_permissions_reuses_cached_approval_without_prompting(monkeypatch):
    monkeypatch.setattr(builtins, 'input', lambda _: (_ for _ in ()).throw(
        AssertionError('should not prompt for a cached approval')))
    cache = {'plugins': {'mod.Foo': 'a==1'}}
    allowed, dirty = _check_plugin_permissions(
        [{'modules': ['mod.Foo'], 'pip': ['a==1']}], 'plugins', cache, 'Transform',
    )
    assert allowed == {'mod.Foo'}
    assert dirty is False


def test_check_plugin_permissions_reprompts_when_pip_deps_change(monkeypatch):
    cache = {'plugins': {'mod.Foo': 'a==1'}}
    monkeypatch.setattr(builtins, 'input', lambda _: 'y')
    allowed, dirty = _check_plugin_permissions(
        [{'modules': ['mod.Foo'], 'pip': ['a==2']}], 'plugins', cache, 'Transform',
    )
    assert allowed == {'mod.Foo'}
    assert dirty is True
    assert cache['plugins']['mod.Foo'] == 'a==2'


def test_check_plugin_permissions_denial_is_not_cached(monkeypatch):
    cache = {}
    monkeypatch.setattr(builtins, 'input', lambda _: 'n')
    allowed, dirty = _check_plugin_permissions(
        [{'modules': ['mod.Foo']}], 'plugins', cache, 'Transform',
    )
    assert allowed == set()
    assert dirty is False
    assert 'plugins' not in cache
