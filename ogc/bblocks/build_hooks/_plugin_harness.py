#!/usr/bin/env python3
"""
Persistent harness for build (lifecycle-hook) plugins.

    python _plugin_harness.py <module_path> <class_name>

Imports `module_path`, instantiates `class_name` once (no-arg constructor),
then services one JSON request per line on stdin for the rest of the process
lifetime:

    {"event": "before_run", "args": {"register": {...}, "context": {...}}}

For each request, dispatches to the same-named method on the instance -
`getattr(instance, event)(**args)` - if the class defines it, or is a no-op
success otherwise (a plugin only implements the events it cares about).
Writes one JSON response per line to stdout:

    {"success": true,  "error": null,  "output": {...} | null, "log": "..." | null}
    {"success": false, "error": "<traceback str>", "output": null, "log": "..." | null}

`output` is only ever meaningful for events whose contract says a plugin may
return a value to feed back into the pipeline (e.g. after_register); for
every other event it is always null and purely informational.

For `before_bblock`/`after_bblock`, the wire request carries a `registerPath` field
instead of an inline `register` (per docs/build-lifecycle-hooks.md's "Payload per
event") - the harness loads that file itself before calling the plugin method, so the
plugin's `before_bblock(self, stage, bblock, register, context)` method still just sees
a plain `register` dict, same shape as every other event.

Mirrors transformers/python.py's persistent-harness precedent: request/response
framing is line-delimited JSON over stdin/stdout, one process serves every call
for its plugin class for the life of the run. Build plugins are expected to be
side-effect-heavy glue code - the most print-prone code in the system - so the
stdout channel is protected in two layers: sys.stdout/sys.stderr are swapped
for a capture buffer around every call (covers Python-level prints), and fd 1
itself is dup2'd to /dev/null at startup (covers a native library writing
straight to the fd, which swapping sys.stdout alone would not catch). The real
stdout fd is duplicated *before* that swap and used to write responses.
"""
import importlib
import io
import json
import os
import sys
import traceback

_BBLOCK_EVENTS = {'before_bblock', 'after_bblock'}


def main():
    module_path, class_name = sys.argv[1], sys.argv[2]

    # Grab a private handle to the real stdout before anything can clobber fd 1.
    real_stdout = os.fdopen(os.dup(1), 'wb')

    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 1)
    os.close(devnull_fd)

    try:
        module = importlib.import_module(module_path)
        instance = getattr(module, class_name)()
        load_error = None
    except Exception:
        instance = None
        load_error = traceback.format_exc()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        event = req['event']
        args = dict(req.get('args') or {})

        if event in _BBLOCK_EVENTS:
            register_path = args.pop('registerPath', None)
            register = None
            if register_path:
                try:
                    with open(register_path) as f:
                        register = json.load(f)
                except Exception:
                    register = None
            args['register'] = register

        capture = io.StringIO()
        prev_stdout, prev_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = capture

        try:
            if load_error is not None:
                raise RuntimeError(f"Failed to load build plugin class "
                                    f"'{module_path}.{class_name}':\n{load_error}")
            method = getattr(instance, event, None)
            output = method(**args) if callable(method) else None
            resp = {'success': True, 'error': None, 'output': output,
                    'log': capture.getvalue() or None}
        except Exception:
            resp = {'success': False, 'error': traceback.format_exc(), 'output': None,
                    'log': capture.getvalue() or None}
        finally:
            sys.stdout, sys.stderr = prev_stdout, prev_stderr

        real_stdout.write((json.dumps(resp) + '\n').encode('utf-8'))
        real_stdout.flush()


if __name__ == '__main__':
    main()
