#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ogc.bblocks.log import log_indent
from ogc.bblocks.models import TransformMetadata, TransformResult, Transformer

logger = logging.getLogger(__name__)

transform_type = 'node'


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


class NodeTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], [], [])

    def transform(self, metadata: TransformMetadata) -> TransformResult:
        node_bin = shutil.which('node')
        if not node_bin:
            return TransformResult(output=None, success=False, stderr="'node' executable not found")

        sandbox_dir = metadata.sandbox_dir
        node_path = str(sandbox_dir / 'node' / 'node_modules') if sandbox_dir else None

        transform_metadata_dict = {
            'sourceMimeType': metadata.source_mime_type,
            'targetMimeType': metadata.target_mime_type,
            'metadata': {k: v for k, v in (metadata.metadata or {}).items()
                         if not k.startswith('_')},
        }

        harness = f"""\
const fs = require('fs');
const transformMetadata = {json.dumps(transform_metadata_dict)};
const inputData = fs.readFileSync(0, 'utf8');
let outputData = null;

const _origStdoutWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = process.stderr.write.bind(process.stderr);

{metadata.transform_content}

process.stdout.write = _origStdoutWrite;
if (outputData !== null) {{
    process.stdout.write(typeof outputData === 'string' ? outputData : Buffer.from(outputData));
}}
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
            f.write(harness)
            harness_path = f.name

        env = os.environ.copy()
        if node_path:
            existing = env.get('NODE_PATH', '')
            env['NODE_PATH'] = f"{node_path}:{existing}" if existing else node_path

        try:
            result = subprocess.run(
                [node_bin, harness_path],
                input=metadata.input_data.encode('utf-8') if isinstance(metadata.input_data, str) else metadata.input_data,
                capture_output=True,
                env=env,
            )
        finally:
            Path(harness_path).unlink(missing_ok=True)

        stderr = result.stderr.decode('utf-8', errors='replace').replace(harness_path, '<transform>') or None
        if stderr:
            label = metadata.id or 'node'
            with log_indent():
                for line in stderr.splitlines():
                    if line.strip():
                        logger.info("[%s] %s", label, line)
        if result.returncode != 0:
            return TransformResult(output=None, success=False, stderr=stderr)

        output, binary = _decode_output(result.stdout)
        return TransformResult(output=output, success=True, stderr=stderr, binary=binary)
