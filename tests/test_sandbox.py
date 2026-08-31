"""Tests for ogc.bblocks.sandbox's pure/near-pure venv-management logic (Tier 1,
docs/pytest-testing-plan.md): pip_slug's deterministic slug generation and
venv_needs_recreate's staleness checks. ensure_venv/pip_install_cached themselves
spawn real subprocesses and aren't covered here.
"""
import re
import sys

from ogc.bblocks.sandbox import _PYTHON_VERSION, pip_slug, venv_needs_recreate


def test_pip_slug_known_value():
    assert pip_slug(['foo==1.0']) == 'foo_1_0'


def test_pip_slug_is_order_independent():
    assert pip_slug(['foo==1.0', 'bar==2.0']) == pip_slug(['bar==2.0', 'foo==1.0'])


def test_pip_slug_only_uses_safe_characters():
    result = pip_slug(['git+https://example.com/x.git@main#egg=x', 'foo==1.0'])
    assert re.fullmatch(r'[a-zA-Z0-9_-]+', result)


def test_pip_slug_empty_list_returns_default():
    assert pip_slug([]) == 'default'


def test_pip_slug_all_special_characters_falls_back_to_default():
    # A spec that's entirely special characters collapses to '' before the
    # "or 'default'" fallback kicks in.
    assert pip_slug(['===']) == 'default'


def _make_pip(venv_dir, shebang_target=None):
    bin_dir = venv_dir / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    pip_bin = bin_dir / 'pip'
    if shebang_target is not None:
        pip_bin.write_text(f'#!{shebang_target}\nimport sys\n')
    else:
        pip_bin.write_text('not-a-shebang-line\n')
    return pip_bin


def test_missing_venv_dir_needs_recreate(tmp_path):
    assert venv_needs_recreate(tmp_path / 'nope') is True


def test_missing_pip_binary_needs_recreate(tmp_path):
    venv_dir = tmp_path / 'venv'
    venv_dir.mkdir()
    assert venv_needs_recreate(venv_dir) is True


def test_shebang_target_missing_needs_recreate(tmp_path):
    venv_dir = tmp_path / 'venv'
    _make_pip(venv_dir, shebang_target=str(tmp_path / 'no-such-python'))
    assert venv_needs_recreate(venv_dir) is True


def test_missing_pyvenv_cfg_needs_recreate(tmp_path):
    venv_dir = tmp_path / 'venv'
    _make_pip(venv_dir, shebang_target=sys.executable)
    assert venv_needs_recreate(venv_dir) is True


def test_matching_python_version_does_not_need_recreate(tmp_path):
    venv_dir = tmp_path / 'venv'
    _make_pip(venv_dir, shebang_target=sys.executable)
    (venv_dir / 'pyvenv.cfg').write_text(f'version = {_PYTHON_VERSION}\n')
    assert venv_needs_recreate(venv_dir) is False


def test_mismatched_python_version_needs_recreate(tmp_path):
    venv_dir = tmp_path / 'venv'
    _make_pip(venv_dir, shebang_target=sys.executable)
    (venv_dir / 'pyvenv.cfg').write_text('version = 2.7.0\n')
    assert venv_needs_recreate(venv_dir) is True


def test_non_shebang_pip_binary_skips_shebang_check(tmp_path):
    venv_dir = tmp_path / 'venv'
    _make_pip(venv_dir, shebang_target=None)
    (venv_dir / 'pyvenv.cfg').write_text(f'version = {_PYTHON_VERSION}\n')
    assert venv_needs_recreate(venv_dir) is False


def test_pyvenv_cfg_without_version_line_does_not_need_recreate(tmp_path):
    venv_dir = tmp_path / 'venv'
    _make_pip(venv_dir, shebang_target=sys.executable)
    (venv_dir / 'pyvenv.cfg').write_text('home = /usr/bin\n')
    assert venv_needs_recreate(venv_dir) is False
