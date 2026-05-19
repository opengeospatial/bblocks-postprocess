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


# Factory function that creates a get_transformer callable for a given subprocess context.
# Included verbatim in every Python context that needs get_transformer (both the persistent
# harness and the one-shot dispatch harness), because it closes over aliased imports
# (_subprocess, _os, _json, _b64, _types, _sys) that are available in both contexts.
#
# Cycle detection uses two mechanisms:
#   _gt_cycle_set: in-process set tracking Python transforms currently executing in this
#                  subprocess (prevents Python→Python in-process cycles).
#   parent_call_stack: list of "bblock_id:transform_id" strings inherited from ancestor
#                      processes via _BBLOCKS_CALL_STACK env var and the 'call_stack' request
#                      field (prevents cross-type and cross-process cycles).
#
# When dispatching non-Python transforms, the child call stack is propagated both via
# the _BBLOCKS_CALL_STACK env var (read by Node's getTransformer) and via the request's
# 'call_stack' field (read by the one-shot Python dispatch harness).
_GET_TRANSFORMER_IMPL = """\
def _make_get_transformer(transforms_registry, main_python, sandbox_base,
                           non_python_harness, parent_call_stack, self_key=None):
    _gt_cycle_set = set()
    _gt_callable_cache = {}
    _gt_compiled_code_cache = {}

    def get_transformer(bblock_id, transform_id):
        key = (bblock_id, transform_id)
        key_str = bblock_id + ':' + transform_id

        if key in _gt_cycle_set:
            raise RuntimeError(
                f'Cycle detected: transform {transform_id!r} of {bblock_id!r} is already executing'
            )
        if key_str in parent_call_stack:
            raise RuntimeError(
                f'Cycle detected: transform {transform_id!r} of {bblock_id!r} is in the call chain'
            )

        if key not in _gt_callable_cache:
            bblock_entry = transforms_registry.get(bblock_id)
            if bblock_entry is None:
                raise KeyError(f'Building block {bblock_id!r} not found in transforms registry')
            transform_entry = bblock_entry['transforms'].get(transform_id)
            if transform_entry is None:
                raise KeyError(
                    f'Transform {transform_id!r} not found for building block {bblock_id!r}'
                )

            t_type = transform_entry.get('type', 'python')

            if t_type == 'python':
                code_str = transform_entry['code']
                if code_str not in _gt_compiled_code_cache:
                    _gt_compiled_code_cache[code_str] = compile(
                        code_str, f'<transform:{bblock_id}/{transform_id}>', 'exec'
                    )
                compiled = _gt_compiled_code_cache[code_str]

                deps = (transform_entry.get('metadata') or {}).get('dependencies', {})
                pip_deps = deps.get('pip', [])
                if isinstance(pip_deps, str):
                    pip_deps = [pip_deps]
                if pip_deps:
                    _subprocess.run(
                        [_sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                         *pip_deps],
                        check=True,
                    )

                def _make_python_callable(
                    _key=key, _bblock_id=bblock_id, _transform_id=transform_id,
                    _compiled=compiled, _entry=transform_entry, _bb=bblock_entry,
                ):
                    def _callable(content, source_mime_type=None, extra_metadata=None):
                        _gt_cycle_set.add(_key)
                        try:
                            _base_meta = dict(_entry.get('metadata') or {})
                            if extra_metadata:
                                _base_meta.update(extra_metadata)
                            _base_meta['_nested_transform'] = True
                            # Use the pre-built context dict from the registry entry.
                            # Fall back to hand-picking fields for old-format registries
                            # (imported from older postprocessor versions without 'context').
                            _bb_ctx = _bb.get('context') or {
                                'bblock_id': _bblock_id,
                                'bblock_name': _bb.get('name'),
                                'bblock_version': (_bb.get('bblock_metadata') or {}).get('version'),
                                'bblock_tags': list((_bb.get('bblock_metadata') or {}).get('tags') or []),
                                'bblock_metadata': _bb.get('bblock_metadata') or {},
                            }
                            _ctx = _types.SimpleNamespace(**_bb_ctx)
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
                            _ns = {
                                'transform_metadata': _tm,
                                'input_data': _input,
                                'output_data': None,
                                'get_transformer': get_transformer,
                            }
                            exec(_compiled, _ns)
                            return _ns.get('output_data')
                        finally:
                            _gt_cycle_set.discard(_key)
                    return _callable

                _gt_callable_cache[key] = _make_python_callable()

            else:
                _sandbox_dir_str = None
                if t_type == 'node' and sandbox_base is not None:
                    deps = (transform_entry.get('metadata') or {}).get('dependencies', {})
                    npm_deps = deps.get('npm', [])
                    if isinstance(npm_deps, str):
                        npm_deps = [npm_deps]
                    if npm_deps:
                        node_dir = _os.path.join(
                            sandbox_base, 'get_transformer',
                            bblock_id.replace('.', '_'), transform_id, 'node',
                        )
                        _os.makedirs(node_dir, exist_ok=True)
                        _subprocess.run(
                            ['npm', 'install', '--prefix', node_dir, *npm_deps],
                            check=True,
                        )
                        _sandbox_dir_str = _os.path.dirname(node_dir)

                def _make_builtin_callable(
                    _key=key, _key_str=key_str, _bblock_id=bblock_id, _transform_id=transform_id,
                    _entry=transform_entry, _type=t_type, _sandbox_dir_str=_sandbox_dir_str,
                ):
                    def _callable(content, source_mime_type=None, extra_metadata=None):
                        _gt_cycle_set.add(_key)
                        try:
                            _base_meta = dict(_entry.get('metadata') or {})
                            if extra_metadata:
                                _base_meta.update(extra_metadata)
                            _base_meta['_nested_transform'] = True

                            if isinstance(content, bytes):
                                _input_bytes = content
                            else:
                                _input_bytes = (content or '').encode('utf-8')

                            # Build call stack for the child subprocess.
                            # Includes the ancestor chain, this process's own identity, all
                            # in-progress Python transforms, and the transform being dispatched.
                            _child_stack = list(parent_call_stack)
                            if self_key and self_key not in _child_stack:
                                _child_stack.append(self_key)
                            for _cs_key in _gt_cycle_set:
                                _cs_str = _cs_key[0] + ':' + _cs_key[1]
                                if _cs_str not in _child_stack:
                                    _child_stack.append(_cs_str)

                            _sub_req = {
                                'type': _type,
                                'transform_content': _entry['code'],
                                'input': _b64.b64encode(_input_bytes).decode(),
                                'source_mime_type': source_mime_type,
                                'target_mime_type': None,
                                'metadata': _base_meta,
                                'bblock_id': _bblock_id,
                                'transform_id': _transform_id,
                                'sandbox_dir': _sandbox_dir_str,
                                'transforms_registry': transforms_registry,
                                'main_python': main_python,
                                'sandbox_base': sandbox_base,
                                'non_python_harness': non_python_harness,
                                'call_stack': _child_stack,
                            }
                            _env = dict(_os.environ)
                            _env['_BBLOCKS_CALL_STACK'] = _json.dumps(_child_stack)
                            _proc = _subprocess.run(
                                [main_python, '-c', non_python_harness],
                                input=_json.dumps(_sub_req).encode('utf-8'),
                                capture_output=True,
                                env=_env,
                            )
                            if not _proc.stdout.strip():
                                _stderr = _proc.stderr.decode('utf-8', errors='replace')
                                raise RuntimeError(
                                    f'get_transformer sub-process for {_type!r} produced no output:\\n{_stderr}'
                                )
                            _resp = _json.loads(_proc.stdout)
                            if not _resp.get('success'):
                                raise RuntimeError(
                                    _resp.get('stderr') or f'{_type!r} sub-transform failed'
                                )
                            _out_b64 = _resp.get('output')
                            if _out_b64 is not None:
                                _out_bytes = _b64.b64decode(_out_b64)
                                return _out_bytes if _resp.get('binary') else _out_bytes.decode('utf-8')
                            return None
                        finally:
                            _gt_cycle_set.discard(_key)
                    return _callable

                _gt_callable_cache[key] = _make_builtin_callable()

        return _gt_callable_cache[key]

    return get_transformer
"""

# One-shot dispatch harness run under the main venv interpreter.
# Handles all supported transform types (python, node, jq, xslt, jsonld-frame).
# Receives a JSON request on stdin, runs the transform, writes a JSON response to stdout.
# Used by: Python's get_transformer() for non-Python types, and Node's getTransformer() for all types.
#
# Includes _GET_TRANSFORMER_IMPL so that Python transforms dispatched from here get a full
# get_transformer callable, enabling arbitrary cross-type chaining.
_NON_PYTHON_HARNESS_CODE = (
    "import sys as _sys, json as _json, base64 as _b64, traceback as _tb,"
    " types as _types, os as _os, subprocess as _subprocess, io as _io\n"
    "from pathlib import Path as _Path\n\n"
) + _GET_TRANSFORMER_IMPL + """

_req = _json.loads(_sys.stdin.buffer.read())
_t_type = _req['type']
_input_bytes = _b64.b64decode(_req['input'])
try:
    _input = _input_bytes.decode('utf-8')
except UnicodeDecodeError:
    _input = _input_bytes

_gt_registry = _req.get('transforms_registry') or {}
_req_bblock_id = _req.get('bblock_id')
_bb_ctx_dict = (_gt_registry.get(_req_bblock_id) or {}).get('context') if _req_bblock_id else None

if _t_type == 'python':
    _gt_main_python = _req.get('main_python') or _sys.executable
    _gt_sandbox_base = _req.get('sandbox_base')
    _gt_non_python_harness = _req.get('non_python_harness') or ''
    _gt_call_stack = list(
        _req.get('call_stack') or
        _json.loads(_os.environ.get('_BBLOCKS_CALL_STACK', '[]'))
    )
    get_transformer = _make_get_transformer(
        _gt_registry, _gt_main_python, _gt_sandbox_base, _gt_non_python_harness,
        _gt_call_stack,
    )
    _meta_dict = dict(_req.get('metadata') or {})
    _ctx_ns = _types.SimpleNamespace(**_bb_ctx_dict) if _bb_ctx_dict else None
    _tm = _types.SimpleNamespace(
        source_mime_type=_req.get('source_mime_type'),
        target_mime_type=_req.get('target_mime_type'),
        metadata=_types.SimpleNamespace(**_meta_dict),
        context=_ctx_ns,
    )
    _ns = {'transform_metadata': _tm, 'input_data': _input, 'output_data': None,
           'get_transformer': get_transformer}
    _capture = _io.StringIO()
    _prev_stdout, _prev_stderr = _sys.stdout, _sys.stderr
    _sys.stdout = _capture
    _sys.stderr = _capture
    try:
        exec(compile(_req['transform_content'], '<transform>', 'exec'), _ns)
        _output = _ns.get('output_data')
        if _output is not None:
            if isinstance(_output, bytes):
                _out_b64 = _b64.b64encode(_output).decode()
                _binary = True
            else:
                _out_b64 = _b64.b64encode(_output.encode('utf-8')).decode()
                _binary = False
        else:
            _out_b64 = None
            _binary = False
        _resp = {'output': _out_b64, 'success': True, 'stderr': _capture.getvalue() or None, 'binary': _binary}
    except Exception:
        _resp = {'output': None, 'success': False, 'stderr': _capture.getvalue() + _tb.format_exc(), 'binary': False}
    finally:
        _sys.stdout, _sys.stderr = _prev_stdout, _prev_stderr
else:
    from ogc.bblocks.models import TransformMetadata as _TM, TransformContext as _TC
    from ogc.bblocks.transformers import transformers as _transformers
    _transformer = _transformers.get(_t_type)
    if _transformer is None:
        _resp = {'output': None, 'success': False,
                 'stderr': f'Unknown transform type: {_t_type!r}', 'binary': False}
    else:
        _sandbox_dir_str = _req.get('sandbox_dir')
        _sandbox_base_str = _req.get('sandbox_base')
        _ctx = _TC(
            **_bb_ctx_dict,
            example_index=0, example={}, snippet_index=0, snippet={},
            output_file=None, output_dir=None, working_dir='',
        ) if _bb_ctx_dict else None
        _meta = _TM(
            type=_t_type,
            source_mime_type=_req.get('source_mime_type'),
            target_mime_type=_req.get('target_mime_type'),
            transform_content=_req['transform_content'],
            input_data=_input,
            metadata=_req.get('metadata') or {},
            sandbox_dir=_Path(_sandbox_dir_str) if _sandbox_dir_str else None,
            id=_req.get('transform_id'),
            ctx=_ctx,
            _transforms_registry=_req.get('transforms_registry') or None,
            _sandbox_base_dir=_Path(_sandbox_base_str) if _sandbox_base_str else None,
        )
        try:
            _result = _transformer.transform(_meta)
            _out_b64 = None
            _binary = False
            if _result.output is not None:
                if isinstance(_result.output, bytes):
                    _out_b64 = _b64.b64encode(_result.output).decode()
                    _binary = True
                else:
                    _out_b64 = _b64.b64encode(_result.output.encode('utf-8')).decode()
            _resp = {'output': _out_b64, 'success': _result.success, 'stderr': _result.stderr, 'binary': _binary}
        except Exception:
            _resp = {'output': None, 'success': False, 'stderr': _tb.format_exc(), 'binary': False}

_sys.stdout.buffer.write((_json.dumps(_resp) + '\\n').encode())
"""

# Static imports line for the persistent harness — avoids brace-escaping inside f-strings.
_HARNESS_IMPORTS = (
    "import sys as _sys, json as _json, types as _types, base64 as _b64,"
    " io as _io, traceback as _tb, os as _os, subprocess as _subprocess\n"
)

# Static body of the persistent harness: get_transformer factory call + the stdin request loop.
# Written as a plain string so braces don't need escaping; dynamic values (_TRANSFORM_CODE,
# _TRANSFORMS_REGISTRY, _MAIN_PYTHON, _SANDBOX_BASE, _NON_PYTHON_HARNESS, _SELF_KEY) are
# injected by _build_persistent_harness via repr() into a header that precedes this block.
# _GET_TRANSFORMER_IMPL (the _make_get_transformer factory) is also spliced in before this block.
_HARNESS_BODY = """
_real_stdout = _sys.stdout.buffer

_parent_call_stack = _json.loads(_os.environ.get('_BBLOCKS_CALL_STACK', '[]'))
get_transformer = _make_get_transformer(
    _TRANSFORMS_REGISTRY, _MAIN_PYTHON, _SANDBOX_BASE, _NON_PYTHON_HARNESS,
    _parent_call_stack, self_key=_SELF_KEY,
)


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

    _ns = {'transform_metadata': transform_metadata, 'input_data': input_data, 'output_data': None,
           'get_transformer': get_transformer}
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
        _resp = {'output': _out_b64, 'success': True, 'stderr': _capture.getvalue() or None, 'binary': _binary}
    except Exception:
        _stderr_text = _capture.getvalue() + _tb.format_exc()
        _resp = {'output': None, 'success': False, 'stderr': _stderr_text, 'binary': False}
    finally:
        _sys.stdout, _sys.stderr = _prev_stdout, _prev_stderr

    _real_stdout.write((_json.dumps(_resp) + '\\n').encode('utf-8'))
    _real_stdout.flush()
"""


def _build_persistent_harness(transform_content: str, transforms_registry: dict,
                               main_python: str, sandbox_base: str | None,
                               self_key: str | None) -> str:
    header = (
        f"_TRANSFORM_CODE = compile({repr(transform_content)}, '<transform>', 'exec')\n"
        f"_TRANSFORMS_REGISTRY = _json.loads({repr(json.dumps(transforms_registry))})\n"
        f"_MAIN_PYTHON = {repr(main_python)}\n"
        f"_SANDBOX_BASE = {repr(sandbox_base)}\n"
        f"_NON_PYTHON_HARNESS = {repr(_NON_PYTHON_HARNESS_CODE)}\n"
        f"_SELF_KEY = {repr(self_key)}\n"
    )
    return _HARNESS_IMPORTS + header + _GET_TRANSFORMER_IMPL + "\n" + _HARNESS_BODY


class _PersistentProcess:

    def __init__(self, python_bin: Path, transform_content: str, transforms_registry: dict,
                 main_python: str, sandbox_base: str | None, self_key: str | None):
        self._python_bin = python_bin
        self._transform_content = transform_content
        self._transforms_registry = transforms_registry
        self._main_python = main_python
        self._sandbox_base = sandbox_base
        self._self_key = self_key
        self._proc: subprocess.Popen | None = None
        self._harness_path: Path | None = None
        self._lock = Lock()
        self._start()

    def _start(self):
        if self._harness_path is None:
            harness = _build_persistent_harness(
                self._transform_content, self._transforms_registry,
                self._main_python, self._sandbox_base, self._self_key,
            )
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

        main_python = str(Path(sys.executable))
        sandbox_base = str(metadata._sandbox_base_dir) if metadata._sandbox_base_dir else None
        self_key = (f"{metadata.ctx.bblock_id}:{metadata.id}"
                    if metadata.ctx and metadata.id else None)

        transforms_registry = metadata._transforms_registry or {}
        cache_key = (str(python_bin), transform_content, main_python, sandbox_base or '',
                     self_key or '')
        with _cache_lock:
            proc = _process_cache.get(cache_key)
            if proc is None:
                proc = _PersistentProcess(python_bin, transform_content, transforms_registry,
                                          main_python, sandbox_base, self_key)
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
