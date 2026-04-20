#!/usr/bin/env python3
"""
Harness for plugin transform types. Two modes:

  Discover:
    python _plugin_harness.py --discover <module_path>

    Scans the module for transformer classes and writes a JSON array to stdout:
      [{"types": [...], "default_inputs": [...], "default_outputs": [...]}, ...]

  Transform:
    python _plugin_harness.py <metadata_json>

    Reads input data from stdin, runs the transform, and writes a JSON object
    to stdout:
      {"success": true,  "output": "<str>",    "binary": false, "stderr": null}
      {"success": true,  "output": "<base64>", "binary": true,  "stderr": null}
      {"success": false, "output": null,        "binary": false, "stderr": "<str>"}

    The metadata object passed to transform() has these attributes:
      type             (str)       transform type identifier
      transform_content (str)      code/script declared in transforms.yaml
      input_data       (str)       example snippet text
      source_mime_type (str)
      target_mime_type (str)
      metadata         (dict)      extra metadata (keys starting with _ excluded)
      sandbox_dir      None        always None in subprocess context
      ctx              SimpleNamespace | None  transform context (bblock, example, register info)
"""
import importlib
import inspect
import json
import sys
import types
from base64 import b64encode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _transformer_classes(module):
    """Yield (cls,) for every transformer class defined in module."""
    module_name = module.__name__
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module_name:
            continue
        types = getattr(cls, 'transform_types', None)
        if types and isinstance(types, list) and all(isinstance(t, str) for t in types):
            yield cls


class _Meta:
    """Minimal TransformMetadata-compatible namespace passed to plugin transform()."""
    __slots__ = ('type', 'transform_content', 'input_data',
                 'source_mime_type', 'target_mime_type', 'metadata', 'sandbox_dir', 'ctx')


# ---------------------------------------------------------------------------
# Discover mode
# ---------------------------------------------------------------------------

def _discover(module_path: str) -> None:
    module = importlib.import_module(module_path)
    result = [
        {
            'class': cls.__name__,
            'types': list(cls.transform_types),
            'default_inputs': list(getattr(cls, 'default_inputs', None) or []),
            'default_outputs': list(getattr(cls, 'default_outputs', None) or []),
        }
        for cls in _transformer_classes(module)
    ]
    print(json.dumps(result))


# ---------------------------------------------------------------------------
# Transform mode
# ---------------------------------------------------------------------------

def _transform(meta_json: str) -> None:
    meta_dict = json.loads(meta_json)

    m = _Meta()
    m.type = meta_dict['type']
    m.transform_content = meta_dict['transform_content']
    m.source_mime_type = meta_dict['source_mime_type']
    m.target_mime_type = meta_dict['target_mime_type']
    m.metadata = meta_dict.get('metadata', {})
    m.input_data = sys.stdin.buffer.read().decode('utf-8')
    m.sandbox_dir = None
    ctx_dict = meta_dict.get('context') or {}
    m.ctx = types.SimpleNamespace(**ctx_dict) if ctx_dict else None

    module_path = meta_dict['module']
    transform_type = meta_dict['type']
    module = importlib.import_module(module_path)

    transformer = next(
        (cls() for cls in _transformer_classes(module)
         if transform_type in cls.transform_types),
        None,
    )

    if transformer is None:
        print(json.dumps({
            'success': False, 'output': None, 'binary': False,
            'stderr': f"No transformer found for type '{transform_type}' in '{module_path}'",
        }))
        return

    try:
        result = transformer.transform(m)
    except Exception as e:
        print(json.dumps({
            'success': False, 'output': None, 'binary': False, 'stderr': str(e),
        }))
        return

    if result is None:
        print(json.dumps({'success': True, 'output': None, 'binary': False, 'stderr': None}))
        return

    if isinstance(result, bytes):
        print(json.dumps({
            'success': True,
            'output': b64encode(result).decode('ascii'),
            'binary': True,
            'stderr': None,
        }))
    else:
        print(json.dumps({'success': True, 'output': result, 'binary': False, 'stderr': None}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: _plugin_harness.py --discover <module> | <metadata_json>',
              file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == '--discover':
        if len(sys.argv) < 3:
            print('Usage: _plugin_harness.py --discover <module>', file=sys.stderr)
            sys.exit(1)
        _discover(sys.argv[2])
    else:
        _transform(sys.argv[1])
