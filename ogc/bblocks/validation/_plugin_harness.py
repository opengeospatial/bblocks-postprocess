#!/usr/bin/env python3
"""
Harness for plugin validator types. Two modes:

  Discover:
    python _plugin_harness.py --discover <module_path>

    Scans the module for validator classes and writes a JSON array to stdout:
      [{"class": "...", "mime_types": [...], "file_extensions": [...]}, ...]

    A validator class is any class defined directly in the module that has a
    non-empty `mime_types` or `file_extensions` class attribute (list of strings)
    and a callable `validate` method.

  Validate:
    python _plugin_harness.py <metadata_json>

    The metadata JSON object has these keys:
      module         (str)   dotted module path
      input_path     (str)   absolute path to the file to validate
      mime_type      (str|null)
      display_filename (str) original filename for use in messages
      schema_ref     (str|null)
      context        (dict)  keys: bblock_id, bblock_name, register_base_url,
                             validation_resources (list of {ref, format[, conformsTo]}
                             where ref is a cwd-relative local path or a URL),
                             bblock_metadata (bblock.json metadata with standard local
                             path fields translated to cwd-relative; custom path fields
                             remain bblock-source-relative)

    Calls validator.validate(meta) where meta is a simple namespace exposing
    the above fields as attributes.

    Writes a JSON object to stdout:
      {"entries": [...], "log": "...", "stderr": null}

    Each entry dict:
      {"message": str, "is_error": bool, "section"?: str, "payload"?: dict}

    Return None or [] from validate() to signal "nothing to report".
"""
import importlib
import inspect
import io
import json
import sys
import traceback
import types


class _MNS:
    def __init__(self, **kw): self.__dict__.update(kw)
    def __getattr__(self, k): return None
    def __repr__(self): return 'namespace(' + ', '.join(f'{k}={v!r}' for k, v in self.__dict__.items()) + ')'


def _validator_classes(module):
    """Yield validator classes defined directly in the module."""
    module_name = module.__name__
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module_name:
            continue
        mime_types = getattr(cls, 'mime_types', None)
        file_extensions = getattr(cls, 'file_extensions', None)
        has_types = (
            (mime_types and isinstance(mime_types, list) and all(isinstance(t, str) for t in mime_types))
            or
            (file_extensions and isinstance(file_extensions, list) and all(isinstance(t, str) for t in file_extensions))
        )
        if has_types and callable(getattr(cls, 'validate', None)):
            yield cls


def _discover(module_path: str) -> None:
    module = importlib.import_module(module_path)
    result = [
        {
            'class': cls.__name__,
            'mime_types': list(getattr(cls, 'mime_types', None) or []),
            'file_extensions': list(getattr(cls, 'file_extensions', None) or []),
        }
        for cls in _validator_classes(module)
    ]
    print(json.dumps(result))


def _validate(meta_json: str) -> None:
    meta_dict = json.loads(meta_json)

    module_path = meta_dict['module']
    class_name = meta_dict.get('class_name')

    meta = _MNS(
        input_path=meta_dict['input_path'],
        mime_type=meta_dict.get('mime_type'),
        display_filename=meta_dict.get('display_filename', ''),
        schema_ref=meta_dict.get('schema_ref'),
        context=_MNS(**(meta_dict.get('context') or {})),
    )

    module = importlib.import_module(module_path)

    validator = next(
        (cls() for cls in _validator_classes(module)
         if class_name is None or cls.__name__ == class_name),
        None,
    )

    if validator is None:
        print(json.dumps({
            'entries': [],
            'log': None,
            'stderr': f"No validator class found in '{module_path}'" + (f" with name '{class_name}'" if class_name else ''),
        }))
        return

    log_buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log_buf
    try:
        result = validator.validate(meta)
        error = None
    except Exception:
        result = None
        error = traceback.format_exc()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr

    log = log_buf.getvalue() or None

    if error is not None:
        print(json.dumps({'entries': [], 'log': log, 'stderr': error}))
        return

    entries = result or []
    if not isinstance(entries, list):
        entries = []

    print(json.dumps({'entries': entries, 'log': log, 'stderr': None}))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: _plugin_harness.py --discover <module> | <metadata_json>', file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == '--discover':
        if len(sys.argv) < 3:
            print('Usage: _plugin_harness.py --discover <module>', file=sys.stderr)
            sys.exit(1)
        _discover(sys.argv[2])
    else:
        _validate(sys.argv[1])
