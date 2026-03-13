#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata, Transformer

transform_type = 'python'


class PythonTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], [], [])

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        sandbox_dir = metadata.sandbox_dir
        if sandbox_dir:
            python_bin = sandbox_dir / 'venv' / 'bin' / 'python'
            if not python_bin.exists():
                python_bin = Path(sys.executable)
        else:
            python_bin = Path(sys.executable)

        transform_metadata_dict = {
            'source_mime_type': metadata.source_mime_type,
            'target_mime_type': metadata.target_mime_type,
            'metadata': {k: v for k, v in (metadata.metadata or {}).items()
                         if not k.startswith('_')},
        }

        harness = f"""\
import sys as _sys
transform_metadata = {json.dumps(transform_metadata_dict)}
input_data = _sys.stdin.read()
output_data = None

{metadata.transform_content}

if output_data is not None:
    _sys.stdout.write(output_data)
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(harness)
            harness_path = f.name

        try:
            result = subprocess.run(
                [str(python_bin), harness_path],
                input=metadata.input_data,
                capture_output=True,
                text=True,
            )
        finally:
            Path(harness_path).unlink(missing_ok=True)

        if result.returncode != 0:
            raise RuntimeError(f"Python transform failed:\n{result.stderr}")

        return result.stdout or None
