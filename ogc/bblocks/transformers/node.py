#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata, Transformer

transform_type = 'node'


class NodeTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], [], [])

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        node_bin = shutil.which('node')
        if not node_bin:
            raise RuntimeError("'node' executable not found")

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

{metadata.transform_content}

if (outputData !== null) {{
    process.stdout.write(outputData);
}}
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
            f.write(harness)
            harness_path = f.name

        env = None
        if node_path:
            import os
            env = os.environ.copy()
            existing = env.get('NODE_PATH', '')
            env['NODE_PATH'] = f"{node_path}:{existing}" if existing else node_path

        try:
            result = subprocess.run(
                [node_bin, harness_path],
                input=metadata.input_data,
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            Path(harness_path).unlink(missing_ok=True)

        if result.returncode != 0:
            raise RuntimeError(f"Node transform failed:\n{result.stderr}")

        return result.stdout or None
