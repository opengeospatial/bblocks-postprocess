from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ogc.bblocks.log import run_logged, log_indent

_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

SANDBOX_DIR_NAME = '.transforms-sandbox'


def venv_needs_recreate(venv_dir: Path) -> bool:
    """Return True if the venv is absent or was built with a different Python version."""
    if not (venv_dir / 'bin' / 'pip').exists():
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
        run_logged([sys.executable, '-m', 'venv', str(venv_dir)], label='venv')
