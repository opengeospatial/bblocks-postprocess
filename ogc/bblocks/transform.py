#!/usr/bin/env python3
from __future__ import annotations

import shutil
import os.path
import subprocess
import sys
from packaging.version import Version
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from pathlib import Path
from urllib.parse import urljoin

from ogc.bblocks import mimetypes
from ogc.bblocks.models import BuildingBlock, TransformMetadata, TransformResult, BuildingBlockError
from ogc.bblocks.transformers import transformers
from ogc.bblocks.util import sanitize_filename

_SUBPROCESS_TRANSFORM_TYPES = ('python', 'node')

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
        print(f"  > Installing pip dependencies: {pip_deps}", file=sys.stderr)
        subprocess.run([str(pip_bin), 'install', '--quiet', '--disable-pip-version-check', *pip_deps], check=True)

    if npm_deps:
        node_dir = sandbox_dir / 'node'
        node_dir.mkdir(exist_ok=True)
        print(f"  > Installing npm dependencies: {npm_deps}", file=sys.stderr)
        subprocess.run(['npm', 'install', '--prefix', str(node_dir), *npm_deps], check=True)


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
                print(f"  > Skipping transform '{transform['id']}': requires Python {constraint} "
                      f"(running {_PYTHON_VERSION})", file=sys.stderr)
                continue
        elif transform.get('type') == 'node':
            node_ver = _get_node_version()
            if (constraint := deps.get('node')) and (not node_ver or not _satisfies(node_ver, constraint)):
                print(f"  > Skipping transform '{transform['id']}': requires Node {constraint} "
                      f"(running {node_ver or 'unknown'})", file=sys.stderr)
                continue

        transformer = transformers.get(transform['type'])
        default_media_types = {
            'inputs': transformer.default_inputs,
            'outputs': transformer.default_outputs,
        } if transformer else None

        # Normalize types
        for io_type in 'inputs', 'outputs':
            io = transform.setdefault(io_type, {})
            media_types = io.get('mediaTypes')
            if not media_types:
                if default_media_types:
                    io['mediaTypes'] = default_media_types[io_type]
                else:
                    io['mediaTypes'] = []
            else:
                io['mediaTypes'] = [(mimetypes.lookup(mt) or mt) if isinstance(mt, str) else mt
                                    for mt in media_types]

        if not transformer or not bblock.examples:
            continue

        supported_input_media_types = {(m if isinstance(m, str) else m['mimeType']): m
                                      for m in transform.get('inputs')['mediaTypes']}
        default_output_media_type: dict | str = next(iter(transform['outputs']['mediaTypes']), None)
        if not default_output_media_type:
            raise BuildingBlockError(f"Transform {transform['id']} for {bblock.identifier}"
                                     f" has no default output formats")
        default_suffix = ('' if isinstance(default_output_media_type, str)
                             else '.' + default_output_media_type['defaultExtension'])
        target_mime_type = (default_output_media_type if isinstance(default_output_media_type, str)
                            else default_output_media_type['mimeType'])

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
