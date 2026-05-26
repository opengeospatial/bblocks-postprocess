#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ogc.bblocks.log import log_indent
from ogc.bblocks.models import TransformMetadata, TransformResult, Transformer
from ogc.bblocks.transformers.python import _NON_PYTHON_HARNESS_CODE

logger = logging.getLogger(__name__)

transform_type = 'node'



class NodeTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], [], [])

    def transform(self, metadata: TransformMetadata) -> TransformResult:
        node_bin = shutil.which('node')
        if not node_bin:
            return TransformResult(output=None, success=False, stderr="'node' executable not found")

        sandbox_dir = metadata.sandbox_dir
        node_path = str(sandbox_dir / 'node' / 'node_modules') if sandbox_dir else None

        transforms_registry = metadata._transforms_registry or {}
        main_python = sys.executable
        sandbox_base = str(metadata._sandbox_base_dir) if metadata._sandbox_base_dir else None
        self_key = (f"{metadata.ctx.bblock_id}:{metadata.id}"
                    if metadata.ctx and metadata.id else None)

        transform_metadata_dict = {
            'sourceMimeType': metadata.source_mime_type,
            'targetMimeType': metadata.target_mime_type,
            'metadata': {k: v for k, v in (metadata.metadata or {}).items()
                         if not k.startswith('_')},
            'context': metadata.ctx.to_dict() if metadata.ctx else None,
        }

        harness = f"""\
const fs = require('fs');
const path = require('path');
const {{spawnSync}} = require('child_process');
const transformMetadata = {json.dumps(transform_metadata_dict)};
const _inputBuf = fs.readFileSync(0);
const inputData = {json.dumps(isinstance(metadata.input_data, bytes))} ? _inputBuf : _inputBuf.toString('utf8');
let outputData = null;

const _TRANSFORMS_REGISTRY = {json.dumps(transforms_registry)};
const _MAIN_PYTHON = {json.dumps(main_python)};
const _SANDBOX_BASE = {json.dumps(sandbox_base)};
const _DISPATCH_HARNESS = {json.dumps(_NON_PYTHON_HARNESS_CODE)};
const _SELF_KEY = {json.dumps(self_key)};

// Call stack inherited from the process that invoked this Node transform (may be empty at root).
const _INITIAL_CALL_STACK = process.env._BBLOCKS_CALL_STACK
    ? JSON.parse(process.env._BBLOCKS_CALL_STACK) : [];

// Execution context of this process: ancestors + self. Used as the base for cycle detection
// and as the child call stack prefix when dispatching sub-transforms.
const _SELF_STACK = (_SELF_KEY && _INITIAL_CALL_STACK.indexOf(_SELF_KEY) === -1)
    ? _INITIAL_CALL_STACK.concat([_SELF_KEY])
    : _INITIAL_CALL_STACK;

const _getTransformerCache = {{}};

function getTransformer(bblockId, transformId) {{
    const cacheKey = bblockId + ':' + transformId;
    if (_getTransformerCache[cacheKey]) return _getTransformerCache[cacheKey];

    const bblockEntry = _TRANSFORMS_REGISTRY[bblockId];
    if (!bblockEntry) throw new Error('Building block ' + JSON.stringify(bblockId) + ' not found in transforms registry');
    const transformEntry = bblockEntry.transforms[transformId];
    if (!transformEntry) throw new Error('Transform ' + JSON.stringify(transformId) + ' not found for building block ' + JSON.stringify(bblockId));

    if (transformEntry.type === 'node' && _SANDBOX_BASE) {{
        let npmDeps = ((transformEntry.metadata || {{}}).dependencies || {{}}).npm || [];
        if (!Array.isArray(npmDeps)) npmDeps = [npmDeps];
        if (npmDeps.length > 0) {{
            const nodeDir = path.join(_SANDBOX_BASE, 'get_transformer',
                bblockId.replace(/\\./g, '_'), transformId, 'node');
            fs.mkdirSync(nodeDir, {{recursive: true}});
            const r = spawnSync('npm', ['install', '--prefix', nodeDir].concat(npmDeps),
                {{encoding: 'utf8'}});
            if (r.status !== 0) throw new Error('npm install failed: ' + (r.stderr || ''));
        }}
    }}

    const fn = function(content, opts) {{
        opts = opts || {{}};
        const sourceMimeType = opts.sourceMimeType || null;
        const extraMetadata = opts.extraMetadata || {{}};

        // Cycle detection: check if the target transform is already in the execution context.
        // _SELF_STACK contains all ancestors (from the env var) plus this Node transform's own key,
        // so cross-type cycles (Python → Node → Python, Node → Python → Node) are caught here.
        if (_SELF_STACK.indexOf(cacheKey) !== -1) {{
            throw new Error('Cycle detected: transform ' + JSON.stringify(transformId) +
                ' of ' + JSON.stringify(bblockId) + ' is already executing');
        }}
        // Child processes see _SELF_STACK plus the transform being dispatched.
        const _childStack = _SELF_STACK.concat([cacheKey]);
        const _childStackStr = JSON.stringify(_childStack);

        const inputBytes = typeof content === 'string'
            ? Buffer.from(content, 'utf8')
            : (Buffer.isBuffer(content) ? content : Buffer.from(String(content)));

        const meta = Object.assign({{}}, transformEntry.metadata || {{}}, extraMetadata,
            {{_nested_transform: true}});

        let sandboxDir = null;
        if (transformEntry.type === 'node' && _SANDBOX_BASE) {{
            let deps = ((transformEntry.metadata || {{}}).dependencies || {{}}).npm || [];
            if (!Array.isArray(deps)) deps = [deps];
            if (deps.length > 0) {{
                sandboxDir = path.join(_SANDBOX_BASE, 'get_transformer',
                    bblockId.replace(/\\./g, '_'), transformId);
            }}
        }}

        const req = {{
            type: transformEntry.type,
            transform_content: transformEntry.code,
            input: inputBytes.toString('base64'),
            source_mime_type: sourceMimeType,
            target_mime_type: null,
            metadata: meta,
            bblock_id: bblockId,
            transform_id: transformId,
            sandbox_dir: sandboxDir,
            transforms_registry: _TRANSFORMS_REGISTRY,
            main_python: _MAIN_PYTHON,
            sandbox_base: _SANDBOX_BASE,
            non_python_harness: _DISPATCH_HARNESS,
            call_stack: _childStack,
        }};

        const spawnEnv = Object.assign({{}}, process.env,
            {{_BBLOCKS_CALL_STACK: _childStackStr}});
        const proc = spawnSync(_MAIN_PYTHON, ['-c', _DISPATCH_HARNESS],
            {{input: Buffer.from(JSON.stringify(req), 'utf8'), encoding: 'buffer', env: spawnEnv}});

        const stdoutStr = (proc.stdout || Buffer.alloc(0)).toString('utf8').trim();
        if (!stdoutStr) {{
            const stderrStr = (proc.stderr || Buffer.alloc(0)).toString('utf8');
            throw new Error('getTransformer sub-process for ' + JSON.stringify(transformEntry.type) +
                ' produced no output\\n' + stderrStr);
        }}
        const resp = JSON.parse(stdoutStr);
        if (!resp.success) throw new Error(resp.stderr || (JSON.stringify(transformEntry.type) + ' sub-transform failed'));
        if (resp.output == null) return null;
        const outBuf = Buffer.from(resp.output, 'base64');
        return resp.binary ? outBuf : outBuf.toString('utf8');
    }};

    _getTransformerCache[cacheKey] = fn;
    return fn;
}}

const _origStdoutWrite = process.stdout.write.bind(process.stdout);
const _origStderrWrite = process.stderr.write.bind(process.stderr);
const _captured = [];
const _captureWrite = (chunk) => {{ _captured.push(Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk)); return true; }};
process.stdout.write = _captureWrite;
process.stderr.write = _captureWrite;

let _success = true, _errMsg = null;
try {{
    {metadata.transform_content}
}} catch (_e) {{
    _success = false;
    _errMsg = (_e && _e.stack) ? _e.stack : String(_e);
}}

process.stdout.write = _origStdoutWrite;
process.stderr.write = _origStderrWrite;

const _capturedStr = _captured.join('') || null;
const _stderrStr = (_capturedStr !== null || _errMsg !== null)
    ? (_capturedStr || '') + (_errMsg ? (_capturedStr ? '\\n' : '') + _errMsg : '')
    : null;

let _outB64 = null, _binary = false;
if (_success && outputData !== null) {{
    const _outBuf = Buffer.isBuffer(outputData)
        ? outputData
        : Buffer.from(typeof outputData === 'string' ? outputData : String(outputData), 'utf8');
    _outB64 = _outBuf.toString('base64');
    _binary = Buffer.isBuffer(outputData);
}}

_origStdoutWrite(JSON.stringify({{output: _outB64, binary: _binary, success: _success, stderr: _stderrStr}}) + '\\n');
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

        label = metadata.id or 'node'

        if not result.stdout.strip():
            stderr = result.stderr.decode('utf-8', errors='replace').replace(harness_path, '<transform>') or 'Node transform produced no output'
            with log_indent():
                for line in stderr.splitlines():
                    if line.strip():
                        logger.info("[%s] %s", label, line)
            return TransformResult(output=None, success=False, stderr=stderr)

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            stderr = result.stderr.decode('utf-8', errors='replace').replace(harness_path, '<transform>') or f'Invalid JSON from Node harness: {e}'
            return TransformResult(output=None, success=False, stderr=stderr)

        stderr = data.get('stderr') or None
        if stderr:
            stderr = stderr.replace(harness_path, '<transform>')
            with log_indent():
                for line in stderr.splitlines():
                    if line.strip():
                        logger.info("[%s] %s", label, line)

        output_b64 = data.get('output')
        if output_b64 is not None:
            output_bytes = base64.b64decode(output_b64)
            output: str | bytes | None = output_bytes if data.get('binary') else output_bytes.decode('utf-8')
        else:
            output = None

        return TransformResult(
            output=output,
            success=data.get('success', False),
            stderr=stderr,
            binary=bool(data.get('binary')),
        )
