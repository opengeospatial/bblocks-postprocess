from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import traceback
from pathlib import Path
from threading import Lock

from ogc.bblocks.log import run_logged, log_indent
from ogc.bblocks.sandbox import ensure_venv, pip_slug
from ogc.bblocks.transform import read_plugin_entries, _pip_to_url
from ogc.bblocks.util import CustomJSONEncoder

logger = logging.getLogger(__name__)

_HARNESS = Path(__file__).parent / '_plugin_harness.py'


class _BuildHookProcess:
    """One persistent subprocess running a single declared build-plugin class.

    Mirrors transformers/python.py's _PersistentProcess: one process per
    (python_bin, class_path), spawned lazily, kept alive for the run, and
    respawned + retried exactly once on a dead pipe (docs/build-lifecycle-hooks.md's
    "Persistent harness: framing and crash recovery" - no hang timeout, no
    circuit breaker, same as that precedent).
    """

    def __init__(self, python_bin: Path, module_path: str, class_name: str):
        self.module_path = module_path
        self.class_name = class_name
        self.class_path = f'{module_path}.{class_name}'
        self._python_bin = python_bin
        self._proc: subprocess.Popen | None = None
        self._lock = Lock()
        self._start()

    def _start(self):
        self._proc = subprocess.Popen(
            [str(self._python_bin), str(_HARNESS), self.module_path, self.class_name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def _send_raw(self, req_line: bytes) -> dict | None:
        try:
            self._proc.stdin.write(req_line)
            self._proc.stdin.flush()
            resp_line = self._proc.stdout.readline()
            if resp_line:
                try:
                    return json.loads(resp_line)
                except json.JSONDecodeError as e:
                    return {
                        'success': False,
                        'error': f'Build plugin harness wrote invalid JSON ({e}): {resp_line!r}',
                        'output': None,
                    }
        except (BrokenPipeError, OSError):
            pass
        return None

    def send(self, event: str, args: dict) -> dict:
        req_line = (json.dumps({'event': event, 'args': args}) + '\n').encode('utf-8')
        with self._lock:
            resp = self._send_raw(req_line)
            if resp is None:
                logger.warning("Build plugin process for '%s' died, respawning", self.class_path)
                self._start()
                resp = self._send_raw(req_line)
            return resp or {
                'success': False,
                'error': f"Build plugin process for '{self.class_path}' died",
                'output': None,
            }

    def close(self):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()


# Keyed by class_path. Module-level so it survives postprocess() returning -
# build plugins are the first plugin kind whose lifetime spans that boundary
# (after_uplift/after_run/on_error fire from entrypoint.py after postprocess()
# has already returned; see docs/build-lifecycle-hooks.md's "Execution model").
_process_cache: dict[str, _BuildHookProcess] = {}
_cache_lock = Lock()


def _close_all_processes():
    with _cache_lock:
        for proc in _process_cache.values():
            proc.close()
        _process_cache.clear()


atexit.register(_close_all_processes)


class BuildPlugin:
    """A single declared build-plugin class (one `classes:` entry): one venv,
    one pooled persistent process, dispatched to by event name."""

    def __init__(self, module_path: str, class_name: str, pip_deps: list[str]):
        self.module_path = module_path
        self.class_name = class_name
        self.class_path = f'{module_path}.{class_name}'
        self.pip_deps = pip_deps

    def _venv_dir(self, sandbox_dir: Path) -> Path:
        return sandbox_dir / 'plugins' / pip_slug(self.pip_deps) / 'venv'

    def ensure_venv(self, sandbox_dir: Path) -> Path:
        venv_dir = self._venv_dir(sandbox_dir)
        if self.pip_deps:
            logger.info("Installing build plugin pip dependencies for '%s': %s",
                        self.class_path, self.pip_deps)
        else:
            logger.info("Setting up build plugin venv for '%s'", self.class_path)
        with log_indent():
            ensure_venv(venv_dir)
            if self.pip_deps:
                pip_bin = venv_dir / 'bin' / 'pip'
                env = os.environ.copy()
                env['GIT_TERMINAL_PROMPT'] = '0'
                env['GIT_ASKPASS'] = 'echo'
                run_logged(
                    [str(pip_bin), 'install', '--disable-pip-version-check', *self.pip_deps],
                    label='pip',
                    env=env,
                )
        return venv_dir

    def _process(self, sandbox_dir: Path) -> _BuildHookProcess:
        with _cache_lock:
            proc = _process_cache.get(self.class_path)
            if proc is None:
                venv_dir = self.ensure_venv(sandbox_dir)
                python_bin = venv_dir / 'bin' / 'python'
                proc = _BuildHookProcess(python_bin, self.module_path, self.class_name)
                _process_cache[self.class_path] = proc
            return proc

    def dispatch(self, sandbox_dir: Path, event: str, args: dict) -> dict:
        """Send one event call to this plugin's harness and return its response dict.

        Does not raise on a failed/dead-process response - the two-tier failure
        rule (run-level checkpoint aborts, per-bblock event follows the core
        fail_on_error pattern) lives in the caller, per
        docs/build-lifecycle-hooks.md's "Failure semantics".
        """
        proc = self._process(sandbox_dir)
        resp = proc.send(event, args)
        log = resp.get('log')
        if log:
            for line in log.splitlines():
                logger.info('[%s] %s', self.class_path, line)
        return resp


# Memoized per sandbox_dir so entrypoint.py calling this a second time (for
# after_uplift/after_run/on_error, once postprocess() has already returned)
# doesn't re-run pip install or re-log setup - it just gets back the same
# (plugins, register_entries), backed by the same pooled processes in
# _process_cache.
_load_cache: dict[str, tuple[list[BuildPlugin], list[dict]]] = {}


def load_build_plugins(sandbox_dir: Path,
                       allowed_classes: set[str] | None = None,
                       ) -> tuple[list[BuildPlugin], list[dict]]:
    """Read plugins.build config, create per-plugin venvs, and return BuildPlugins.

    Reads from ``plugins.build`` in bblocks-config.yaml (via read_plugin_entries,
    no transform-plugins.yml-style legacy fallback for this section).

    allowed_classes: if provided, only install/register classes in this set
    (as returned by permissions.check_build_plugin_permissions). Pass None to
    allow all - i.e. under --skip-permissions.

    Returns a tuple of (build_plugins, register_entries), mirroring
    validate.load_validation_plugins: build_plugins is what dispatch_* calls
    against, register_entries is the enriched list suitable for inclusion in
    register.json under 'buildPlugins'.
    """
    cache_key = str(sandbox_dir.resolve())
    cached = _load_cache.get(cache_key)
    if cached is not None:
        return cached

    plugins: list[BuildPlugin] = []
    register_entries: list[dict] = []

    for plugin in read_plugin_entries('build'):
        pip_deps = plugin.get('pip', [])
        if isinstance(pip_deps, str):
            pip_deps = [pip_deps]

        classes = plugin.get('classes', [])
        if isinstance(classes, str):
            classes = [classes]

        output_classes = []

        for class_path in classes:
            if allowed_classes is not None and class_path not in allowed_classes:
                logger.info("Skipping build plugin '%s': not permitted by user", class_path)
                continue
            module_path, sep, class_name = class_path.rpartition('.')
            if not sep:
                logger.warning(
                    "Invalid build plugin class path (expected 'module.ClassName'): %s", class_path)
                continue
            plugins.append(BuildPlugin(module_path, class_name, pip_deps))
            output_classes.append(class_path)

        if output_classes:
            output_entry = {'classes': output_classes}
            original_pip = plugin.get('pip')
            if original_pip:
                output_entry['pip'] = original_pip
                if explicit_url := plugin.get('url'):
                    output_entry['urls'] = [explicit_url]
                else:
                    urls = [u for s in pip_deps for u in [_pip_to_url(s)] if u]
                    if urls:
                        output_entry['urls'] = urls
            register_entries.append(output_entry)

    result = (plugins, register_entries)
    _load_cache[cache_key] = result
    return result


def _json_safe(obj):
    """Round-trip *obj* through CustomJSONEncoder so it's safe to send as plain
    JSON over the harness wire - register dicts can hold sets/Path/PathOrUrl
    values that plain json.dumps can't handle on its own."""
    return json.loads(json.dumps(obj, cls=CustomJSONEncoder))


def _dispatch_checkpoint(plugins: list[BuildPlugin], sandbox_dir: Path, event: str, args: dict) -> None:
    """Fire *event* on every loaded build plugin, in declaration order.

    Shared by the run-level checkpoints with no mutation contract (before_run,
    after_uplift, after_run): a failing call always aborts the whole run,
    unconditionally, regardless of --fail-on-error (see "Failure semantics").
    after_register is handled separately below since it alone may mutate the
    register passed to the next plugin.
    """
    for plugin in plugins:
        resp = plugin.dispatch(sandbox_dir, event, args)
        if not resp.get('success'):
            raise RuntimeError(
                f"{event} failed in build plugin '{plugin.class_path}': {resp.get('error')}")


def dispatch_before_run(plugins: list[BuildPlugin], sandbox_dir: Path,
                        register: dict, context: dict) -> None:
    """Fire before_run(register, context) on every loaded build plugin."""
    _dispatch_checkpoint(plugins, sandbox_dir, 'before_run', {'register': register, 'context': context})


def dispatch_after_register(plugins: list[BuildPlugin], sandbox_dir: Path,
                            register: dict, context: dict) -> dict:
    """Fire after_register(register, context) on every loaded build plugin, in
    declaration order, chaining each plugin's returned register into the next.

    after_register is the one deliberate mutation point (see the design doc):
    if a plugin's response 'output' is a dict, it replaces the register seen by
    the next plugin - and, ultimately, what gets persisted to register.json. A
    plugin that returns nothing (the common case - just observing) leaves the
    register as-is for the next plugin.

    Run-level checkpoint: a failing call always aborts the whole run,
    unconditionally, regardless of --fail-on-error.
    """
    current = register
    for plugin in plugins:
        resp = plugin.dispatch(sandbox_dir, 'after_register',
                               {'register': _json_safe(current), 'context': context})
        if not resp.get('success'):
            raise RuntimeError(
                f"after_register failed in build plugin '{plugin.class_path}': {resp.get('error')}")
        output = resp.get('output')
        if isinstance(output, dict):
            current = output
    return current


def dispatch_after_uplift(plugins: list[BuildPlugin], sandbox_dir: Path,
                          register: dict | None, context: dict) -> None:
    """Fire after_uplift(register, context) on every loaded build plugin."""
    _dispatch_checkpoint(plugins, sandbox_dir, 'after_uplift', {'register': register, 'context': context})


def dispatch_after_run(plugins: list[BuildPlugin], sandbox_dir: Path,
                       register: dict | None, context: dict) -> None:
    """Fire after_run(register, context) on every loaded build plugin.

    Fires on success only - mutually exclusive with dispatch_on_error, which
    the caller must ensure fires instead on an aborted run.
    """
    _dispatch_checkpoint(plugins, sandbox_dir, 'after_run', {'register': register, 'context': context})


def dispatch_on_error(plugins: list[BuildPlugin], sandbox_dir: Path,
                      error: BaseException, register: dict | None, context: dict,
                      phase: str) -> None:
    """Fire on_error(error, register, context) on every loaded build plugin.

    Mutually exclusive with dispatch_after_run - the caller must ensure exactly
    one of the two is dispatched per run. Unlike the other checkpoints, a
    failure here is logged and never re-dispatched (recursion guard) rather
    than raised: preserving "exactly one of after_run/on_error fires" is worth
    more than covering the corner where on_error itself misbehaves.
    """
    error_payload = {
        'type': type(error).__name__,
        'message': str(error),
        'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__)),
        'phase': phase,
    }
    for plugin in plugins:
        try:
            resp = plugin.dispatch(sandbox_dir, 'on_error',
                                   {'error': error_payload, 'register': register, 'context': context})
            if not resp.get('success'):
                logger.error("on_error failed in build plugin '%s': %s",
                            plugin.class_path, resp.get('error'))
        except Exception:
            logger.exception("Error dispatching on_error to build plugin '%s'", plugin.class_path)
