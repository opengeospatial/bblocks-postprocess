from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from ogc.bblocks.log import run_logged, log_indent
from ogc.bblocks.sandbox import ensure_venv, pip_slug
from ogc.bblocks.validation import ValidationReportEntry, ValidationReportItem, ValidationReportSection

logger = logging.getLogger(__name__)


def _to_wire_path(ref: str) -> str:
    """Convert an absolute local path to cwd-relative for the subprocess wire format.

    Mirrors the _rel() convention used in the transform context: cwd-relative when
    the path is under the cwd, absolute otherwise (e.g. system temp files). URLs
    are passed through unchanged.
    """
    if ref.startswith(('http://', 'https://', 'ftp://')):
        return ref
    try:
        return os.path.relpath(ref)
    except ValueError:
        return ref  # Windows cross-drive path — keep absolute

_HARNESS = Path(__file__).parent / '_plugin_harness.py'
_PLUGIN_TIMEOUT = 120


class PluginValidator:
    """Runs an external validator plugin in an isolated subprocess venv.

    Unlike built-in Validator subclasses, PluginValidator is instantiated once
    globally (not per building block). Building block context is passed per-call
    via the subprocess metadata dict.

    Plugin classes are identified by duck-typing: any class in the module that
    has a non-empty ``mime_types`` or ``file_extensions`` class attribute and a
    callable ``validate`` method.
    """

    def __init__(self, module_path: str, class_name: str,
                 pip_deps: list[str], sandbox_dir: Path,
                 mime_types: list[str], file_extensions: list[str]):
        self.module_path = module_path
        self.class_name = class_name
        self.pip_deps = pip_deps
        self.sandbox_dir = sandbox_dir
        self.mime_types = [m.lower() for m in mime_types]
        self.file_extensions = [
            e.lower() if e.startswith('.') else f'.{e.lower()}'
            for e in file_extensions
        ]

    def _venv_dir(self) -> Path:
        return self.sandbox_dir / 'plugins' / pip_slug(self.pip_deps) / 'venv'

    @staticmethod
    def ensure_venv_for(pip_deps: list[str], sandbox_dir: Path) -> Path:
        """Create/update the venv for *pip_deps* and return its path."""
        venv_dir = sandbox_dir / 'plugins' / pip_slug(pip_deps) / 'venv'
        with log_indent():
            ensure_venv(venv_dir)
            if pip_deps:
                pip_bin = venv_dir / 'bin' / 'pip'
                env = os.environ.copy()
                env['GIT_TERMINAL_PROMPT'] = '0'
                env['GIT_ASKPASS'] = 'echo'
                run_logged(
                    [str(pip_bin), 'install', '--disable-pip-version-check', *pip_deps],
                    label='pip',
                    env=env,
                )
        return venv_dir

    def ensure_venv(self) -> Path:
        if self.pip_deps:
            logger.info("Installing validator plugin pip dependencies for '%s': %s",
                        self.module_path, self.pip_deps)
        else:
            logger.info("Setting up validator plugin venv for '%s'", self.module_path)
        return self.ensure_venv_for(self.pip_deps, self.sandbox_dir)

    @staticmethod
    def discover(venv_dir: Path, module_path: str) -> list[dict] | None:
        """Run --discover and return the list of validator class descriptors.

        Returns None if the subprocess failed (module cannot be imported or harness
        crashed), or a (possibly empty) list if discovery succeeded.
        """
        python_bin = venv_dir / 'bin' / 'python'
        result = subprocess.run(
            [str(python_bin), str(_HARNESS), '--discover', module_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error(
                "Validator plugin discovery failed for '%s' (exit %d):\n%s",
                module_path, result.returncode,
                (result.stderr or result.stdout or '(no output)').strip(),
            )
            return None
        try:
            return json.loads(result.stdout.strip())
        except Exception:
            logger.error(
                "Validator plugin discovery for '%s' returned invalid JSON: %s",
                module_path, result.stdout[:500],
            )
            return None

    def _matches(self, file_format: str | None, filename: Path | None) -> bool:
        if file_format:
            fmt = file_format.lower().split(';')[0].strip()
            if fmt in self.mime_types:
                return True
        if filename:
            ext = filename.suffix.lower()
            if ext in self.file_extensions:
                return True
        return False

    def validate(self,
                 filename: Path,
                 output_filename: Path,
                 report: ValidationReportItem,
                 contents: str | bytes | None = None,
                 file_format: str | None = None,
                 **kwargs) -> bool | None:
        if not self._matches(file_format, filename):
            return False

        venv_dir = self._venv_dir()
        python_bin = venv_dir / 'bin' / 'python'

        bblock = kwargs.get('bblock')
        bblocks_register = kwargs.get('bblocks_register')
        context = {}
        if bblock is not None:
            context = {
                'bblock_id': bblock.identifier,
                'bblock_name': getattr(bblock, 'name', None),
                'register_base_url': getattr(bblocks_register, 'base_url', None),
                'validation_resources': [
                    {**r, 'ref': _to_wire_path(r['ref'])} if r.get('ref') else r
                    for r in (kwargs.get('validation_resources') or [])
                ],
                'bblock_metadata': getattr(bblock, 'pre_baseurl_metadata', None),
            }

        temp_file = None
        try:
            if contents is not None:
                suffix = filename.suffix or ''
                if not suffix and file_format:
                    import mimetypes as _mimetypes
                    guessed = _mimetypes.guess_extension(file_format.split(';')[0].strip())
                    if guessed:
                        suffix = guessed
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    temp_file = Path(tf.name)
                    if isinstance(contents, str):
                        tf.write(contents.encode('utf-8'))
                    else:
                        tf.write(contents)
                input_path = str(temp_file)
            else:
                input_path = str(filename.resolve())

            meta_dict = {
                'module': self.module_path,
                'class_name': self.class_name,
                'input_path': input_path,
                'mime_type': file_format,
                'display_filename': filename.name,
                'schema_ref': kwargs.get('schema_ref'),
                'context': context,
            }

            try:
                proc = subprocess.run(
                    [str(python_bin), str(_HARNESS), json.dumps(meta_dict)],
                    capture_output=True, text=True,
                    timeout=_PLUGIN_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.PLUGIN,
                    message=f"Validator plugin '{self.module_path}' timed out after {_PLUGIN_TIMEOUT}s",
                    is_error=True,
                    payload={'plugin': f"{self.module_path}.{self.class_name}"},
                ))
                return True

            try:
                data = json.loads(proc.stdout)
            except Exception:
                stderr = proc.stderr or 'Unexpected harness error (no JSON output)'
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.PLUGIN,
                    message=f"Validator plugin '{self.module_path}' harness error: {stderr}",
                    is_error=True,
                    payload={'plugin': f"{self.module_path}.{self.class_name}"},
                ))
                return True

            if data.get('log'):
                for line in data['log'].splitlines():
                    logger.debug('[validator-plugin] %s', line)

            if data.get('stderr'):
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.PLUGIN,
                    message=f"Validator plugin '{self.module_path}' error: {data['stderr']}",
                    is_error=True,
                    payload={'plugin': f"{self.module_path}.{self.class_name}"},
                ))
                return True

            plugin_id = f"{self.module_path}.{self.class_name}"
            entries = data.get('entries') or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                payload = dict(entry.get('payload') or {})
                payload['plugin'] = plugin_id
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.PLUGIN,
                    message=entry.get('message', ''),
                    is_error=bool(entry.get('is_error', False)),
                    payload=payload,
                ))

            return True if entries else None

        finally:
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
