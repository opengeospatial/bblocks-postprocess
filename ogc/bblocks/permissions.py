from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from ogc.bblocks.transform import _PERMISSION_CHECKED_TYPES as _RISKY_TRANSFORM_TYPES
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


def _require_tty() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Transform permission check required but stdin is not a TTY. "
            "Run with --skip-permissions true to bypass permission checks "
            "in non-interactive environments (e.g. CI)."
        )


def _ask_yes_no(prompt: str) -> bool:
    _require_tty()
    while True:
        answer = input(f"{prompt} [y/N] ").strip().lower()
        if answer in ('y', 'yes'):
            return True
        if answer in ('', 'n', 'no'):
            return False
        print("  Please answer y or n.")


def _read_plugin_configs() -> list[dict]:
    """Read transform-plugins.yml without installing anything."""
    plugins_path = Path('transform-plugins.yml')
    if not plugins_path.exists():
        return []
    try:
        with open(plugins_path) as f:
            config = yaml.safe_load(f)
        if not config or 'plugins' not in config:
            return []
        return config.get('plugins', []) or []
    except Exception:
        return []


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


def check_permissions(
    sandbox_dir: Path,
    items_dir: Path,
) -> tuple[set[str], set[str]]:
    """Check and prompt for permissions for risky transforms and plugins.

    Must be called before load_transform_plugins and before apply_transforms.
    Returns:
        allowed_transform_types: set of approved type strings
        allowed_plugin_modules:  set of approved module paths
    Raises RuntimeError if stdin is not a TTY and permissions are needed.
    """
    cache = _load_cache(sandbox_dir)
    cache_dirty = False

    cached_types: set[str] = set(cache.get('transform-types', []))
    cached_plugins: dict[str, str] = dict(cache.get('plugins', {}))

    # --- Transform types ---
    needed_types = _scan_risky_transforms(items_dir)
    unapproved_types = {t: blocks for t, blocks in needed_types.items() if t not in cached_types}

    if unapproved_types:
        _require_tty()
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

    # --- Plugins ---
    plugin_configs = _read_plugin_configs()
    allowed_plugin_modules: set[str] = set()

    for plugin in plugin_configs:
        modules = plugin.get('modules', [])
        if isinstance(modules, str):
            modules = [modules]
        version_key = _plugin_version_key(plugin)
        pip_deps = plugin.get('pip', [])
        if isinstance(pip_deps, str):
            pip_deps = [pip_deps]

        for module in modules:
            if cached_plugins.get(module) == version_key:
                allowed_plugin_modules.add(module)
                continue

            _require_tty()
            print()
            print(f"╔══ Plugin permission required")
            print(f"║ Plugin: {module}")
            if pip_deps:
                print(f"║ Dependencies: {', '.join(pip_deps)}")
            print()
            if _ask_yes_no(f"Allow plugin '{module}' to be installed and run?"):
                cached_plugins[module] = version_key
                allowed_plugin_modules.add(module)
                cache['plugins'] = cached_plugins
                cache_dirty = True

    if cache_dirty:
        _save_cache(sandbox_dir, cache)

    return allowed_transform_types, allowed_plugin_modules