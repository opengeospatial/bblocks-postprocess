from __future__ import annotations

import json
import subprocess
import sys
from base64 import b64decode
from pathlib import Path

from ogc.bblocks.models import TransformMetadata, TransformResult

_HARNESS = Path(__file__).parent / '_plugin_harness.py'


class PluginTransformer:

    def __init__(self, module_path: str, pip_deps: list[str], transform_types: list[str]):
        self.module_path = module_path
        self.pip_deps = pip_deps
        self.transform_types = transform_types
        self.default_inputs: list = []
        self.default_outputs: list = []

    def ensure_venv(self, sandbox_dir: Path) -> Path:
        slug = self.module_path.replace('.', '_')
        venv_dir = sandbox_dir / 'plugins' / slug / 'venv'
        if not venv_dir.exists():
            print(f"  > Setting up plugin venv for '{self.module_path}'"
                  + (f" (pip: {self.pip_deps})" if self.pip_deps else ""),
                  file=sys.stderr)
            subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True)
            if self.pip_deps:
                pip_bin = venv_dir / 'bin' / 'pip'
                subprocess.run(
                    [str(pip_bin), 'install', '--quiet',
                     '--disable-pip-version-check', *self.pip_deps],
                    check=True,
                )
        return venv_dir

    @staticmethod
    def discover(venv_dir: Path, module_path: str) -> list[dict]:
        """Run --discover and return the list of transformer class descriptors."""
        python_bin = venv_dir / 'bin' / 'python'
        result = subprocess.run(
            [str(python_bin), str(_HARNESS), '--discover', module_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout.strip())
        except Exception:
            return []

    def transform(self, metadata: TransformMetadata) -> TransformResult:
        if not metadata.sandbox_dir:
            return TransformResult(
                output=None, success=False,
                stderr='Plugin transforms require a sandbox directory',
            )

        venv_dir = self.ensure_venv(metadata.sandbox_dir)
        python_bin = venv_dir / 'bin' / 'python'

        meta_dict = {
            'type': metadata.type,
            'module': self.module_path,
            'transform_content': metadata.transform_content,
            'source_mime_type': metadata.source_mime_type,
            'target_mime_type': metadata.target_mime_type,
            'metadata': {k: v for k, v in (metadata.metadata or {}).items()
                         if not k.startswith('_')},
        }

        result = subprocess.run(
            [str(python_bin), str(_HARNESS), json.dumps(meta_dict)],
            input=(metadata.input_data.encode('utf-8')
                   if isinstance(metadata.input_data, str)
                   else metadata.input_data),
            capture_output=True,
        )

        try:
            data = json.loads(result.stdout)
        except Exception:
            stderr = result.stderr.decode('utf-8', errors='replace') or 'Unexpected harness error'
            return TransformResult(output=None, success=False, stderr=stderr)

        output = data.get('output')
        if output is not None and data.get('binary'):
            output = b64decode(output)

        return TransformResult(
            output=output,
            success=data.get('success', False),
            stderr=data.get('stderr') or None,
            binary=bool(data.get('binary')),
        )
