#!/usr/bin/env python3
from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from ogc.bblocks import mimetypes
from ogc.bblocks.models import BuildingBlock, TransformMetadata, BuildingBlockError
from ogc.bblocks.transformers import transformers


def apply_transforms(bblock: BuildingBlock,
                     outputs_path: str | Path,
                     output_subpath='transforms'):

    if not bblock.transforms:
        return

    output_dir = Path(outputs_path) / bblock.subdirs / output_subpath
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for transform in bblock.transforms:

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

                output_fn = output_dir / (f"example_{example_id + 1}_{snippet_id + 1}"
                                          f".{transform['id']}{default_suffix}")

                metadata = transform.get('metadata', {})
                metadata['_prefixes'] = example_prefixes

                transform_metadata = TransformMetadata(type=transform['type'],
                                                       source_mime_type=snippet_mime_type,
                                                       target_mime_type=target_mime_type,
                                                       transform_content=transform['code'],
                                                       metadata=metadata,
                                                       input_data=snippet['code'])

                try:
                    transform_result = transformer.transform(transform_metadata)
                    if transform_result:
                        with open(output_fn, 'w') as f:
                            f.write(transform_result)
                except:
                    with open(output_fn.with_stem(output_fn.name + '.error'), 'w') as f:
                        f.write('Error generating transformed file:\n')
                        f.write(traceback.format_exc())
