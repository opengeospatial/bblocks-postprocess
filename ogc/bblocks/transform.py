#!/usr/bin/env python3
from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from ogc.bblocks.models import BuildingBlock, TransformMetadata
from ogc.bblocks import mimetypes
from ogc.bblocks import transformers


def apply_transforms(bblock: BuildingBlock,
                     outputs_path: str | Path,
                     output_subpath='transforms'):
    if not bblock.examples:
        return

    output_dir = Path(outputs_path) / bblock.subdirs / output_subpath
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for example_id, example in enumerate(bblock.examples):
        transforms = example.get('transforms')
        snippets = example.get('snippets')
        if not transforms or not snippets:
            continue

        transforms_by_input_lang = {}
        for idx, transform in enumerate(transforms):
            transform['input-language'] = mimetypes.normalize(transform['input-language'])
            output_lang = mimetypes.lookup(transform['output-language'])
            if output_lang:
                transform['output-extension'] = output_lang['extensions'][0]
                transform['output-language'] = output_lang['mime-type']
            else:
                transform['output-extension'] = transform['output-language']

            transform['idx'] = idx
            transforms_by_input_lang.setdefault(transform['input-language'], []).append(transform)

        for snippet_id, snippet in enumerate(snippets):
            snippet_lang = snippet.get('language')
            if not snippet_lang:
                continue
            snippet_mime_type = mimetypes.normalize(snippet_lang)

            for transform in transforms_by_input_lang.get(snippet_mime_type, ()):
                output_ext = transform['output-extension']
                output_fn = output_dir / (f"example_{example_id + 1}_{snippet_id + 1}"
                                          f"-{transform['idx'] + 1}.{output_ext}")

                transform_metadata = TransformMetadata(type=transform['type'],
                                                       source_mime_type=transform['input-language'],
                                                       target_mime_type=transform['output-language'],
                                                       transform_content=transform['code'],
                                                       metadata=transform.get('metadata'),
                                                       input_data=snippet['code'])

                try:
                    transform_result = transformers.transform(transform_metadata)
                    if transform_result:
                        with open(output_fn, 'w') as f:
                            f.write(transform_result)

                except:
                    with open(output_fn.with_stem(output_fn.name + '.error'), 'w') as f:
                        f.write('Error generating transformed file:\n')
                        f.write(traceback.format_exc())
