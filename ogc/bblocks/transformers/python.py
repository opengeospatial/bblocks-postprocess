#!/usr/bin/env python3
from __future__ import annotations

import atexit
import base64
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Lock

from ogc.bblocks.log import log_indent
from ogc.bblocks.models import TransformMetadata, TransformResult, Transformer

logger = logging.getLogger(__name__)

transform_type = 'python'

_process_cache: dict[tuple, _PersistentProcess] = {}
_cache_lock = Lock()


def _build_persistent_harness(transform_content: str, transforms_registry: dict) -> str:
    registry_json = json.dumps(transforms_registry)
    return f"""\
import sys as _sys, json as _json, types as _types, base64 as _b64, io as _io, traceback as _tb

_TRANSFORM_CODE = compile({repr(transform_content)}, '<transform>', 'exec')
_TRANSFORMS_REGISTRY = _json.loads({repr(registry_json)})

_real_stdout = _sys.stdout.buffer

# Module-level state for get_transformer
_cycle_set = set()
_callable_cache = {{}}
_compiled_code_cache = {{}}


def get_transformer(bblock_id, transform_id):
    key = (bblock_id, transform_id)
    if key in _cycle_set:
        raise RuntimeError(
            f"Cycle detected: transform {{transform_id!r}} of {{bblock_id!r}} is already executing"
        )

    if key not in _callable_cache:
        bblock_entry = _TRANSFORMS_REGISTRY.get(bblock_id)
        if bblock_entry is None:
            raise KeyError(f"Building block {{bblock_id!r}} not found in transforms registry")
        transform_entry = bblock_entry['transforms'].get(transform_id)
        if transform_entry is None:
            raise KeyError(
                f"Transform {{transform_id!r}} not found for building block {{bblock_id!r}}"
            )

        code_str = transform_entry['code']
        if code_str not in _compiled_code_cache:
            _compiled_code_cache[code_str] = compile(
                code_str, f'<transform:{{bblock_id}}/{{transform_id}}>', 'exec'
            )
        compiled = _compiled_code_cache[code_str]

        deps = (transform_entry.get('metadata') or {{}}).get('dependencies', {{}})
        pip_deps = deps.get('pip', [])
        if isinstance(pip_deps, str):
            pip_deps = [pip_deps]
        if pip_deps:
            import subprocess as _subproc
            _subproc.run(
                [_sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', *pip_deps],
                check=True,
            )

        def _make_callable(_key=key, _bblock_id=bblock_id, _transform_id=transform_id,
                           _compiled=compiled, _entry=transform_entry, _bb=bblock_entry):
            def _callable(content, extra_metadata=None):
                _cycle_set.add(_key)
                try:
                    _base_meta = dict(_entry.get('metadata') or {{}})
                    if extra_metadata:
                        _base_meta.update(extra_metadata)
                    _base_meta['_nested_transform'] = True

                    _ctx = _types.SimpleNamespace(
                        bblock_id=_bblock_id,
                        bblock_name=_bb.get('name'),
                        bblock_version=_bb.get('bblock_metadata', {{}}).get('version'),
                        bblock_tags=_bb.get('bblock_metadata', {{}}).get('tags', []),
                        bblock_metadata=_bb.get('bblock_metadata', {{}}),
                    )
                    _tm = _types.SimpleNamespace(
                        source_mime_type=None,
                        target_mime_type=None,
                        metadata=_types.SimpleNamespace(**_base_meta),
                        context=_ctx,
                    )

                    if isinstance(content, bytes):
                        try:
                            _input = content.decode('utf-8')
                        except UnicodeDecodeError:
                            _input = content
                    else:
                        _input = content

                    _ns = {{
                        'transform_metadata': _tm,
                        'input_data': _input,
                        'output_data': None,
                        'get_transformer': get_transformer,
                    }}
                    exec(_compiled, _ns)
                    return _ns.get('output_data')
                finally:
                    _cycle_set.discard(_key)
            return _callable

        _callable_cache[key] = _make_callable()

    return _callable_cache[key]


for _line in _sys.stdin:
    _line = _line.strip()
    if not _line:
        continue
    _req = _json.loads(_line)
    _d = _req['metadata']
    if isinstance(_d.get('context'), dict):
        _d['context'] = _types.SimpleNamespace(**_d['context'])
    transform_metadata = _types.SimpleNamespace(**_d)
    _raw = _b64.b64decode(_req['input'])
    try:
        input_data = _raw.decode('utf-8')
    except UnicodeDecodeError:
        input_data = _raw

    _capture = _io.StringIO()
    _prev_stdout, _prev_stderr = _sys.stdout, _sys.stderr
    _sys.stdout = _capture
    _sys.stderr = _capture

    _ns = {{'transform_metadata': transform_metadata, 'input_data': input_data, 'output_data': None,
            'get_transformer': get_transformer}}
    try:
        exec(_TRANSFORM_CODE, _ns)
        output_data = _ns.get('output_data')
        if output_data is not None:
            if isinstance(output_data, bytes):
                _out_b64 = _b64.b64encode(output_data).decode()
                _binary = True
            else:
                _out_b64 = _b64.b64encode(output_data.encode('utf-8')).decode()
                _binary = False
        else:
            _out_b64 = None
            _binary = False
        _resp = {{'output': _out_b64, 'success': True, 'stderr': _capture.getvalue() or None, 'binary': _binary}}
    except Exception:
        _stderr_text = _capture.getvalue() + _tb.format_exc()
        _resp = {{'output': None, 'success': False, 'stderr': _stderr_text, 'binary': False}}
    finally:
        _sys.stdout, _sys.stderr = _prev_stdout, _prev_stderr

    _real_stdout.write((_json.dumps(_resp) + '\\n').encode('utf-8'))
    _real_stdout.flush()
"""


class _PersistentProcess:

    def __init__(self, python_bin: Path, transform_content: str, transforms_registry: dict):
        self._python_bin = python_bin
        self._transform_content = transform_content
        self._transforms_registry = transforms_registry
        self._proc: subprocess.Popen | None = None
        self._harness_path: Path | None = None
        self._lock = Lock()
        self._start()

    def _start(self):
        if self._harness_path is None:
            harness = _build_persistent_harness(self._transform_content, self._transforms_registry)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(harness)
                self._harness_path = Path(f.name)
        self._proc = subprocess.Popen(
            [str(self._python_bin), str(self._harness_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def _send_raw(self, req_line: bytes) -> dict | None:
        try:
            self._proc.stdin.write(req_line)
            self._proc.stdin.flush()
            resp_line = self._proc.stdout.readline()
            if resp_line:
                return json.loads(resp_line)
        except (BrokenPipeError, OSError):
            pass
        return None

    def send(self, metadata_dict: dict, input_data: bytes | str) -> dict:
        if isinstance(input_data, str):
            input_data = input_data.encode('utf-8')
        req_line = (json.dumps({
            'metadata': metadata_dict,
            'input': base64.b64encode(input_data).decode(),
        }) + '\n').encode('utf-8')

        with self._lock:
            resp = self._send_raw(req_line)
            if resp is None:
                logger.warning("Transform process died, respawning")
                self._start()
                resp = self._send_raw(req_line)
            return resp or {'output': None, 'success': False, 'stderr': 'Transform process died', 'binary': False}

    def close(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
            if self._harness_path:
                self._harness_path.unlink(missing_ok=True)
                self._harness_path = None


def _close_all_processes():
    with _cache_lock:
        for proc in _process_cache.values():
            proc.close()
        _process_cache.clear()


atexit.register(_close_all_processes)


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

        transform_content = metadata.transform_content
        if isinstance(transform_content, bytes):
            transform_content = transform_content.decode('utf-8')

        transforms_registry = metadata.transforms_registry or {}
        cache_key = (str(python_bin), transform_content)
        with _cache_lock:
            proc = _process_cache.get(cache_key)
            if proc is None:
                proc = _PersistentProcess(python_bin, transform_content, transforms_registry)
                _process_cache[cache_key] = proc

        transform_metadata_dict = {
            'source_mime_type': metadata.source_mime_type,
            'target_mime_type': metadata.target_mime_type,
            'metadata': {k: v for k, v in (metadata.metadata or {}).items()
                         if not k.startswith('_')},
            'context': metadata.ctx.to_dict() if metadata.ctx else None,
        }

        resp = proc.send(transform_metadata_dict, metadata.input_data)

        stderr = resp.get('stderr') or None
        if stderr:
            label = metadata.id or 'python'
            with log_indent():
                for line in stderr.splitlines():
                    if line.strip():
                        logger.info("[%s] %s", label, line)

        output_b64 = resp.get('output')
        if output_b64 is not None:
            output_bytes = base64.b64decode(output_b64)
            output: str | bytes | None = output_bytes if resp.get('binary') else output_bytes.decode('utf-8')
        else:
            output = None

        return TransformResult(
            output=output,
            success=resp.get('success', False),
            stderr=stderr,
            binary=resp.get('binary', False),
        )
