from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from ogc.bblocks.transform import _PERMISSION_CHECKED_TYPES as _RISKY_TRANSFORM_TYPES, read_plugin_entries

logger = logging.getLogger(__name__)

_PERMISSIONS_FILE = 'permissions.json'


def _load_cache(sandbox_dir: Path) -> dict:
    cache_file = sandbox_dir / _PERMISSIONS_FILE
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(sandbox_dir: Path, cache: dict) -> None:
    cache_file = sandbox_dir / _PERMISSIONS_FILE
    with open(cache_file, 'w') as f:
        json.dump(cache, f, indent=2)


def _ask_yes_no(prompt: str) -> bool:
    """Ask a y/n question.

    If stdin has no interactive input to offer (e.g. `docker run` without `-i`),
    `input()` hits EOF immediately rather than blocking, so we treat that as a
    denial instead of crashing the whole run.
    """
    while True:
        try:
            answer = input(f"{prompt} [y/N] ").strip().lower()
        except EOFError:
            logger.warning(
                "No interactive input available to answer this prompt (stdin is closed) "
                "- denying by default. Run `docker run -i ...` to answer prompts interactively, "
                "or pass --skip-permissions true to bypass permission checks entirely."
            )
            return False
        if answer in ('y', 'yes'):
            return True
        if answer in ('', 'n', 'no'):
            return False
        print("  Please answer y or n.")




def _scan_risky_transforms(items_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Lightweight scan for bblocks containing risky transform types.

    Returns a dict mapping type -> [(bblock_id, bblock_name), ...]
    """
    needed: dict[str, list[tuple[str, str]]] = {}
    for bblock_json in items_dir.rglob('bblock.json'):
        bblock_dir = bblock_json.parent
        transforms_file = bblock_dir / 'transforms.yaml'
        if not transforms_file.exists():
            transforms_file = bblock_dir / 'transforms.yml'
        if not transforms_file.exists():
            continue
        try:
            raw = yaml.safe_load(transforms_file.read_text()) or {}
            transforms = raw.get('transforms', []) if isinstance(raw, dict) else []
            bblock_data = json.loads(bblock_json.read_text())
            bblock_id = bblock_data.get('identifier', str(bblock_dir))
            bblock_name = bblock_data.get('name', bblock_id)
            for t in transforms:
                t_type = (t or {}).get('type')
                if t_type in _RISKY_TRANSFORM_TYPES:
                    needed.setdefault(t_type, []).append((bblock_id, bblock_name))
        except Exception:
            pass
    return needed


def _plugin_version_key(plugin: dict) -> str:
    """Stable cache key representing the plugin's pip dependencies."""
    pip = plugin.get('pip', [])
    if isinstance(pip, str):
        pip = [pip]
    return ','.join(sorted(pip))


def _check_plugin_permissions(
    plugin_entries: list[dict],
    cache_key: str,
    cache: dict,
    label: str,
) -> tuple[set[str], bool]:
    """Prompt for permissions for a list of plugin entries.

    Returns (allowed_modules, cache_was_modified).
    """
    cached: dict[str, str] = dict(cache.get(cache_key, {}))
    allowed: set[str] = set()
    dirty = False

    for plugin in plugin_entries:
        modules = plugin.get('modules', [])
        if isinstance(modules, str):
            modules = [modules]
        version_key = _plugin_version_key(plugin)
        pip_deps = plugin.get('pip', [])
        if isinstance(pip_deps, str):
            pip_deps = [pip_deps]

        for module in modules:
            if cached.get(module) == version_key:
                allowed.add(module)
                continue

            print()
            print(f"╔══ {label} plugin permission required")
            print(f"║ Plugin: {module}")
            if pip_deps:
                print(f"║ Dependencies: {', '.join(pip_deps)}")
            print()
            if _ask_yes_no(f"Allow {label.lower()} plugin '{module}' to be installed and run?"):
                cached[module] = version_key
                allowed.add(module)
                cache[cache_key] = cached
                dirty = True

    return allowed, dirty


def check_permissions(
    sandbox_dir: Path,
    items_dir: Path,
) -> tuple[set[str], set[str], set[str]]:
    """Check and prompt for permissions for risky transforms and plugins.

    Must be called before load_transform_plugins, load_validation_plugins,
    and apply_transforms.
    Returns:
        allowed_transform_types:    set of approved type strings
        allowed_plugin_modules:     set of approved transform plugin module paths
        allowed_validator_modules:  set of approved validator plugin module paths
    Denies (with a warning) any permission that can't be answered interactively,
    e.g. when stdin is closed because `docker run` was invoked without `-i`.
    """
    cache = _load_cache(sandbox_dir)
    cache_dirty = False

    cached_types: set[str] = set(cache.get('transform-types', []))

    # --- Transform types ---
    needed_types = _scan_risky_transforms(items_dir)
    unapproved_types = {t: blocks for t, blocks in needed_types.items() if t not in cached_types}

    if unapproved_types:
        print()
        print("╔══ Transform permission required")
        print("║ The following building blocks contain transforms that can execute arbitrary code on your machine:")
        for t_type in sorted(unapproved_types):
            print(f"║")
            print(f"║  [{t_type.upper()}]")
            for bb_id, bb_name in unapproved_types[t_type]:
                print(f"║    • {bb_id}  ({bb_name})")
        print("║")
        if _ask_yes_no("Allow these transforms to run?"):
            for t_type in unapproved_types:
                cached_types.add(t_type)
            cache['transform-types'] = sorted(cached_types)
            cache_dirty = True

    allowed_transform_types = cached_types

    # --- Transform plugins ---
    allowed_plugin_modules, dirty = _check_plugin_permissions(
        read_plugin_entries('transforms'), 'plugins', cache, 'Transform',
    )
    cache_dirty = cache_dirty or dirty

    # --- Validator plugins ---
    allowed_validator_modules, dirty = _check_plugin_permissions(
        read_plugin_entries('validators'), 'validator-plugins', cache, 'Validator',
    )
    cache_dirty = cache_dirty or dirty

    if cache_dirty:
        _save_cache(sandbox_dir, cache)

    return allowed_transform_types, allowed_plugin_modules, allowed_validator_modules