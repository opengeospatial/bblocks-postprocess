"""Coverage for postprocess._git_last_modified, the helper backing the "review your
generated OpenAPI document" warning for extensionPoints declarations that predate
OpenAPI extension-point processing going from declarative-only to actually merging/
substituting the document (see OAS_EXTENSION_PROCESSING_SINCE in postprocess.py).
"""
import datetime
import os
import subprocess

from ogc.bblocks.postprocess import _git_last_modified


def _git(cwd, *args, env=None):
    full_env = {**os.environ, **(env or {})}
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, env=full_env)


def test_git_last_modified_returns_commit_date(tmp_path, monkeypatch):
    # _git_last_modified (like do_postprocess's own git-log call it mirrors) doesn't
    # pass cwd= to subprocess.run - it relies on the process cwd already being inside
    # the checkout, same as in production (entrypoint.py always runs from repo root).
    monkeypatch.chdir(tmp_path)

    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.email', 'test@example.com')
    _git(tmp_path, 'config', 'user.name', 'Test')

    f = tmp_path / 'openapi.yaml'
    f.write_text('openapi: 3.1.0\n')
    _git(tmp_path, 'add', 'openapi.yaml')
    commit_date = datetime.datetime(2020, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _git(tmp_path, 'commit', '-q', '-m', 'initial', env={
        'GIT_AUTHOR_DATE': commit_date.isoformat(),
        'GIT_COMMITTER_DATE': commit_date.isoformat(),
    })

    assert _git_last_modified(f) == commit_date.date()


def test_git_last_modified_returns_none_for_untracked_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, 'init', '-q')
    assert _git_last_modified(tmp_path / 'never-committed.yaml') is None


def test_git_last_modified_returns_none_outside_git_repo(tmp_path, monkeypatch):
    # tmp_path itself is not a git checkout
    monkeypatch.chdir(tmp_path)
    assert _git_last_modified(tmp_path / 'openapi.yaml') is None
