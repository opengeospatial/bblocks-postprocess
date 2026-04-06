#!/usr/bin/env python3
from __future__ import annotations

import logging
import re
import shutil
import os.path
import subprocess
import sys

logger = logging.getLogger(__name__)
from packaging.version import Version
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from pathlib import Path
from urllib.parse import urljoin

import yaml

from ogc.bblocks import mimetypes
from ogc.bblocks.models import BuildingBlock, TransformMetadata, TransformResult, BuildingBlockError
from ogc.bblocks.transformers import transformers
from ogc.bblocks.util import sanitize_filename

_SUBPROCESS_TRANSFORM_TYPES = ('python', 'node')


def _pip_to_url(pip_spec: str) -> str | None:
    """Derive a human-facing URL from a pip install specifier, or None if not applicable."""
    if not pip_spec:
        return None
    # Local paths — no meaningful URL
    if pip_spec.startswith(('/', './', '../')):
        return None
    # Git URL: git+https://.../.git[@ref]
    if pip_spec.startswith('git+'):
        url = pip_spec[4:]                        # strip git+
        url = re.sub(r'@[^@]*$', '', url)         # strip @ref
        url = re.sub(r'\.git$', '', url.rstrip('/'))  # strip .git
        return url
    # Plain archive/wheel URL
    if pip_spec.startswith(('https://', 'http://')):
        return pip_spec
    # Standard package name, possibly with version specifier or extras
    name = re.split(r'[>=<!~\[@\s]', pip_spec)[0].strip()
    if name:
        return f'https://pypi.org/project/{name}'
    return None

_PLUGINS_FILE = 'transform-plugins.yml'



def _normalize_media_type(mt: str | dict) -> dict:
    if isinstance(mt, str):
        entry = mimetypes.lookup(mt)
        if entry:
            result = {'mimeType': entry['mimeType']}
            if 'label' in entry:
                result['label'] = entry['label']
            if 'defaultExtension' in entry:
                result['defaultExtension'] = entry['defaultExtension']
            return result
        return {'mimeType': mimetypes.normalize(mt)}
    else:
        result = dict(mt)
        result['mimeType'] = mimetypes.normalize(mt.get('mimeType', ''))
        if 'label' not in result:
            entry = mimetypes.lookup(result['mimeType'])
            if entry and 'label' in entry:
                result['label'] = entry['label']
        return result

_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
_node_version_cache: str | None = None


def _get_node_version() -> str | None:
    global _node_version_cache
    if _node_version_cache is None:
        try:
            result = subprocess.run(['node', '--version'], capture_output=True, text=True)
            _node_version_cache = result.stdout.strip().lstrip('v')
        except Exception:
            _node_version_cache = ''
    return _node_version_cache or None


def _satisfies(current_version: str, constraint: str) -> bool:
    try:
        return Version(current_version) in SpecifierSet(constraint)
    except (InvalidSpecifier, Exception):
        return True  # unparseable constraint: don't block


def _ensure_sandbox(sandbox_dir: Path, bblock: BuildingBlock) -> None:
    """Install any dependencies declared in this bblock's transforms into the sandbox."""
    pip_deps = []
    npm_deps = []

    for transform in bblock.transforms:
        deps = (transform.get('metadata') or {}).get('dependencies', {})
        if pip := deps.get('pip'):
            pip_deps.extend(pip if isinstance(pip, list) else [pip])
        if npm := deps.get('npm'):
            npm_deps.extend(npm if isinstance(npm, list) else [npm])

    if pip_deps:
        venv_dir = sandbox_dir / 'venv'
        if not venv_dir.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True)
        pip_bin = venv_dir / 'bin' / 'pip'
        logger.info("Installing pip dependencies: %s", pip_deps)
        subprocess.run([str(pip_bin), 'install', '--quiet', '--disable-pip-version-check', *pip_deps], check=True)

    if npm_deps:
        node_dir = sandbox_dir / 'node'
        node_dir.mkdir(exist_ok=True)
        logger.info("Installing npm dependencies: %s", npm_deps)
        subprocess.run(['npm', 'install', '--prefix', str(node_dir), *npm_deps], check=True)


def load_transform_plugins(sandbox_dir: Path) -> list[dict]:
    """Read transform-plugins.yml, create per-plugin venvs, and register PluginTransformers.

    Returns the raw plugin list from transform-plugins.yml (for inclusion in register.json),
    or an empty list if the file does not exist or declares no plugins.
    """
    from ogc.bblocks.transformers.plugin import PluginTransformer

    plugins_path = Path(_PLUGINS_FILE)
    if not plugins_path.exists():
        return []

    with open(plugins_path) as f:
        config = yaml.safe_load(f)

    if not config or 'plugins' not in config:
        return []

    output_plugins = []

    for plugin in config.get('plugins', []):
        pip_deps = plugin.get('pip', [])
        if isinstance(pip_deps, str):
            pip_deps = [pip_deps]

        modules = plugin.get('modules', [])
        if isinstance(modules, str):
            modules = [modules]

        output_modules = []

        for module_path in modules:
            # Create venv and run discovery via the harness
            venv_dir = PluginTransformer(module_path, pip_deps, []).ensure_venv(sandbox_dir)
            discovered = PluginTransformer.discover(venv_dir, module_path)

            if not discovered:
                logger.warning("No transform types found in plugin '%s'", module_path)
                continue

            output_transformers = []
            for entry in discovered:
                types = entry.get('types', [])
                if not types:
                    continue
                pt = PluginTransformer(module_path, pip_deps, types)
                pt.default_inputs = entry.get('default_inputs', [])
                pt.default_outputs = entry.get('default_outputs', [])
                logger.info("Registered plugin '%s' (%s) for types: %s",
                            module_path, entry.get('class', '?'), types)
                for tt in types:
                    transformers[tt] = pt
                output_transformers.append({
                    'class': entry.get('class'),
                    'types': types,
                    'defaultInputs': pt.default_inputs,
                    'defaultOutputs': pt.default_outputs,
                })

            if output_transformers:
                output_modules.append({'module': module_path, 'transformers': output_transformers})

        if output_modules:
            output_entry = {'modules': output_modules}
            original_pip = plugin.get('pip')
            if original_pip:
                output_entry['pip'] = original_pip
                if explicit_url := plugin.get('url'):
                    output_entry['urls'] = [explicit_url]
                else:
                    urls = [u for s in pip_deps for u in [_pip_to_url(s)] if u]
                    if urls:
                        output_entry['urls'] = urls
            output_plugins.append(output_entry)

    return output_plugins


def apply_transforms(bblock: BuildingBlock,
                     outputs_path: str | Path,
                     output_subpath='transforms',
                     base_url: str | None = None,
                     sandbox_dir: Path | None = None):

    if not bblock.transforms:
        return

    cwd = Path().resolve()
    output_dir = Path(outputs_path) / bblock.subdirs / output_subpath
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Install dependencies for subprocess transforms before processing any snippets
    if sandbox_dir and any(t.get('type') in _SUBPROCESS_TRANSFORM_TYPES for t in bblock.transforms):
        _ensure_sandbox(sandbox_dir, bblock)

    for transform in bblock.transforms:

        deps = (transform.get('metadata') or {}).get('dependencies', {})
        if transform.get('type') == 'python':
            if (constraint := deps.get('python')) and not _satisfies(_PYTHON_VERSION, constraint):
                logger.info("Skipping transform '%s': requires Python %s (running %s)",
                            transform['id'], constraint, _PYTHON_VERSION)
                continue
        elif transform.get('type') == 'node':
            node_ver = _get_node_version()
            if (constraint := deps.get('node')) and (not node_ver or not _satisfies(node_ver, constraint)):
                logger.info("Skipping transform '%s': requires Node %s (running %s)",
                            transform['id'], constraint, node_ver or 'unknown')
                continue

        transformer = transformers.get(transform['type'])
        default_media_types = {
            'inputs': getattr(transformer, 'default_inputs', []),
            'outputs': getattr(transformer, 'default_outputs', []),
        } if transformer else None

        # Normalize types
        for io_type in 'inputs', 'outputs':
            io = transform.setdefault(io_type, {})
            media_types = io.get('mediaTypes')
            if not media_types:
                if default_media_types:
                    io['mediaTypes'] = [_normalize_media_type(mt) for mt in default_media_types[io_type]]
                else:
                    io['mediaTypes'] = []
            else:
                io['mediaTypes'] = [_normalize_media_type(mt) for mt in media_types]

        if not transformer or not bblock.examples:
            continue

        supported_input_media_types = {m['mimeType']: m
                                      for m in transform.get('inputs')['mediaTypes']}
        default_output_media_type: dict | str = next(iter(transform['outputs']['mediaTypes']), None)
        if not default_output_media_type:
            raise BuildingBlockError(f"Transform {transform['id']} for {bblock.identifier}"
                                     f" has no default output formats")
        if 'defaultExtension' in default_output_media_type:
            default_suffix = '.' + default_output_media_type['defaultExtension']
        else:
            default_suffix = ''
            logger.warning("Output media type '%s' for transform %s in %s has no known file extension;"
                           " output files will have no extension. Declare a 'defaultExtension' to avoid this.",
                           default_output_media_type['mimeType'], transform['id'], bblock.identifier)
        target_mime_type = default_output_media_type['mimeType']

        bblock_prefixes = bblock.example_prefixes or {}

        for example_id, example in enumerate(bblock.examples):
            snippets = example.get('snippets')
            if not snippets:
                continue

            example_prefixes = bblock_prefixes | example.get('prefixes', {})

            for snippet_id, snippet in enumerate(snippets):
                snippet_lang = snippet.get('language')
                if not snippet_lang:
                    continue
                snippet_mime_type = mimetypes.normalize(snippet_lang)

                if snippet_mime_type not in supported_input_media_types:
                    continue

                if base_output_filename := example.get('base-output-filename'):
                    output_fn = output_dir / sanitize_filename(base_output_filename)
                    output_fn = output_fn.with_name(f"{output_fn.stem}.{transform['id']}{default_suffix}")
                else:
                    output_fn = output_dir / (f"example_{example_id + 1}_{snippet_id + 1}"
                                              f".{transform['id']}{default_suffix}")

                metadata = transform.get('metadata', {})
                if example_prefixes:
                    metadata['_prefixes'] = example_prefixes

                transform_metadata = TransformMetadata(type=transform['type'],
                                                       source_mime_type=snippet_mime_type,
                                                       target_mime_type=target_mime_type,
                                                       transform_content=transform['code'],
                                                       metadata=metadata,
                                                       input_data=snippet['code'],
                                                       sandbox_dir=sandbox_dir)

                try:
                    result = transformer.transform(transform_metadata)
                except Exception as e:
                    result = TransformResult(output=None, success=False,
                                             stderr=f"{type(e).__name__}: {e}")

                entry = {'success': result.success}
                if result.stderr:
                    entry['stderr'] = result.stderr
                if result.binary:
                    entry['binary'] = True
                if result.output:
                    mode = 'wb' if result.binary else 'w'
                    with open(output_fn, mode) as f:
                        f.write(result.output)
                    output_rel_path = str(os.path.relpath(output_fn, cwd))
                    if base_url:
                        output_rel_path = urljoin(base_url, output_rel_path)
                    entry['url'] = output_rel_path
                snippet_transform_results = snippet.setdefault('transformResults', {})
                snippet_transform_results[transform['id']] = entry
