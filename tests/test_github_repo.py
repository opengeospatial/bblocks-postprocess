"""Tests for ogc.bblocks.util.get_github_repo, used to autodetect --base-url / --github-base-url
from the git remote."""
import pytest

from ogc.bblocks.util import get_github_repo


@pytest.mark.parametrize('url', [
    'https://github.com/org/repo.git',
    'https://github.com/org/repo',
    'https://github.com/org/repo/',
    'https://www.github.com/org/repo',
    'http://github.com/org/repo.git',
    'git@github.com:org/repo.git',
    'git@github.com:org/repo',
])
def test_github_remote_forms(url):
    assert get_github_repo(url) == ('org', 'repo')


def test_dotted_repo_name_keeps_dots():
    assert get_github_repo('https://github.com/org/my.repo.git') == ('org', 'my.repo')
    assert get_github_repo('https://github.com/org/my.repo') == ('org', 'my.repo')


@pytest.mark.parametrize('url', [
    None,
    '',
    'https://gitlab.com/org/repo.git',
    'https://github.com/org',
    'https://github.com/org/repo/tree/main',
])
def test_non_github_remotes(url):
    assert get_github_repo(url) is None
