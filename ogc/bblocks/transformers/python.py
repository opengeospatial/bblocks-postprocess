#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from ogc.bblocks.models import TransformMetadata, TransformResult, Transformer

logger = logging.getLogger(__name__)

transform_type = 'python'


def _strip_harness_frames(stderr: str, harness_path: str) -> str:
    lines = stderr.splitlines(keepends=True)
    result = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if f'File "{harness_path}"' in line:
            skip_next = True
            continue
        result.append(line)
    return ''.join(result)


def _decode_output(raw: bytes) -> tuple[str | bytes | None, bool]:
    if not raw:
        return None, False
    try:
        text = raw.decode('utf-8')
        if '\x00' in text:
            return raw, True
        return text, False
    except UnicodeDecodeError:
        return raw, True


class PythonTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], [], [])

    def transform(self, metadata: TransformMetadata) -> TransformResult:
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
_real_stdout = _sys.stdout
_sys.stdout = _sys.stderr
exec(compile({repr(metadata.transform_content)}, '<transform>', 'exec'), globals())
_sys.stdout = _real_stdout
if output_data is not None:
    _sys.stdout.buffer.write(output_data if isinstance(output_data, bytes) else output_data.encode('utf-8'))
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(harness)
            harness_path = f.name

        try:
            result = subprocess.run(
                [str(python_bin), harness_path],
                input=metadata.input_data.encode('utf-8') if isinstance(metadata.input_data, str) else metadata.input_data,
                capture_output=True,
            )
        finally:
            Path(harness_path).unlink(missing_ok=True)

        stderr = _strip_harness_frames(result.stderr.decode('utf-8', errors='replace'), harness_path) or None
        if stderr:
            label = metadata.id or 'python'
            for line in stderr.splitlines():
                if line.strip():
                    logger.info("[%s] %s", label, line)
        if result.returncode != 0:
            return TransformResult(output=None, success=False, stderr=stderr)

        output, binary = _decode_output(result.stdout)
        return TransformResult(output=output, success=True, stderr=stderr, binary=binary)
