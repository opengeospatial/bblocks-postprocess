from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ogc.bblocks.log import run_logged, log_indent

logger = logging.getLogger(__name__)

_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

SANDBOX_DIR_NAME = '.bblocks-sandbox'
_OLD_SANDBOX_DIR_NAME = '.transforms-sandbox'

_GIT_LOCK_FILE_NAME = '.git-pip-lock.json'

# Matches git pip specifiers: git+<url>[@ref][#fragment]
_GIT_PIP_RE = re.compile(
    r'^git\+(?P<url>[^@#\s]+?)(?:@(?P<ref>[^#\s]+))?(?:#.*)?$',
    re.IGNORECASE,
)
_FULL_SHA_RE = re.compile(r'^[0-9a-f]{40}$', re.IGNORECASE)


def pip_slug(pip_deps: list[str]) -> str:
    """Return a stable, human-readable directory name for a set of pip specifiers.

    The slug identifies the venv that would result from installing exactly these
    deps. Same deps → same slug → shared venv. Different version → different slug.
    """
    key = ','.join(sorted(pip_deps))
    slug = re.sub(r'[^a-zA-Z0-9_-]+', '_', key)
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug or 'default'


def venv_needs_recreate(venv_dir: Path) -> bool:
    """Return True if the venv is absent, broken, or was built with a different Python version."""
    pip_bin = venv_dir / 'bin' / 'pip'
    if not pip_bin.exists():
        return True
    # Verify pip's shebang target still exists (catches renamed sandbox dirs and removed interpreters).
    try:
        first_line = pip_bin.read_text(errors='replace').split('\n', 1)[0]
        if first_line.startswith('#!'):
            shebang_target = Path(first_line[2:].strip().split()[0])
            if not shebang_target.exists():
                return True
    except OSError:
        return True
    cfg = venv_dir / 'pyvenv.cfg'
    if not cfg.exists():
        return True
    for line in cfg.read_text().splitlines():
        if line.startswith('version'):
            _, _, ver = line.partition('=')
            return ver.strip() != _PYTHON_VERSION
    return False


def ensure_venv(venv_dir: Path) -> None:
    """Create or recreate the venv at venv_dir if needed."""
    if venv_needs_recreate(venv_dir):
        shutil.rmtree(venv_dir, ignore_errors=True)
        # Wipe git dep lock so changed Python forces a full reinstall.
        (venv_dir.parent / _GIT_LOCK_FILE_NAME).unlink(missing_ok=True)
        run_logged([sys.executable, '-m', 'venv', str(venv_dir)], label='venv')


def _resolve_git_dep_commit(dep: str, env: dict) -> str | None:
    """Return the resolved HEAD commit SHA for a git pip dep, or None on failure.

    If the ref in the specifier is already a full 40-hex SHA it is returned as-is
    without a network call. Otherwise git ls-remote is used to resolve the ref.
    For annotated tags the dereferenced commit (^{} entry) is preferred.
    """
    m = _GIT_PIP_RE.match(dep)
    if not m:
        return None
    url, ref = m.group('url'), m.group('ref')
    if ref and _FULL_SHA_RE.match(ref):
        return ref  # Already pinned to a specific commit.
    ls_ref = ref or 'HEAD'
    try:
        result = subprocess.run(
            ['git', 'ls-remote', url, ls_ref],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        commits: dict[str, str] = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split('\t', 1)
            if len(parts) == 2:
                commits[parts[1]] = parts[0]
        # Prefer dereferenced annotated tag commit.
        for refname, sha in commits.items():
            if refname.endswith('^{}'):
                return sha
        return next(iter(commits.values()), None)
    except Exception:
        return None


def pip_install_cached(venv_dir: Path, pip_deps: list[str]) -> None:
    """Run pip install, skipping git deps whose resolved commit SHA is unchanged.

    Resolved SHAs are stored in <venv_dir.parent>/.git-pip-lock.json, which
    ensure_venv() deletes whenever the venv is rebuilt so stale entries never
    prevent a necessary reinstall.
    """
    if not pip_deps:
        return

    git_env = os.environ.copy()
    git_env['GIT_TERMINAL_PROMPT'] = '0'
    git_env['GIT_ASKPASS'] = 'echo'

    lock_file = venv_dir.parent / _GIT_LOCK_FILE_NAME
    lock: dict[str, str] = {}
    if lock_file.exists():
        try:
            lock = json.loads(lock_file.read_text())
        except Exception:
            pass

    skip: set[str] = set()
    new_commits: dict[str, str] = {}

    for dep in pip_deps:
        if not _GIT_PIP_RE.match(dep):
            continue
        commit = _resolve_git_dep_commit(dep, git_env)
        if commit is None:
            continue  # Can't resolve — let pip handle it normally.
        new_commits[dep] = commit
        if lock.get(dep) == commit:
            logger.debug("Skipping git pip dep '%s': commit %s unchanged", dep, commit[:12])
            skip.add(dep)

    if skip:
        skipped_str = ', '.join(f"'{d}'" for d in skip)
        logger.info("Git pip dep(s) up to date, skipping reinstall: %s", skipped_str)

    deps_to_install = [d for d in pip_deps if d not in skip]
    if deps_to_install:
        pip_bin = venv_dir / 'bin' / 'pip'
        run_logged(
            [str(pip_bin), 'install', '--disable-pip-version-check', *deps_to_install],
            label='pip',
            env=git_env,
        )

    if new_commits:
        try:
            lock_file.write_text(json.dumps({**lock, **new_commits}, indent=2))
        except Exception:
            pass
