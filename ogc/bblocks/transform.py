#!/usr/bin/env python3
from __future__ import annotations

import shutil
import traceback
from pathlib import Path

from ogc.na.util import is_url

from ogc.bblocks.util import BuildingBlock, TransformMetadata
from ogc.bblocks import mimetypes
from ogc.bblocks import transformers


def apply_transforms(bblock: BuildingBlock,
                     outputs_path: str | Path,
                     output_subpath='transforms'):
    if not bblock.examples or not bblock.transforms:
        return

    output_dir = Path(outputs_path) / bblock.subdirs / output_subpath
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    transforms_per_type = {}
    for transform in bblock.transforms:
        for i, mt in enumerate(transform['mime-types']['source']):
            source_mime_type = mimetypes.lookup(mt)
            if source_mime_type:
                mt = source_mime_type['mime-type']
                transform['mime-types']['source'][i] = mt
            transforms_per_type.setdefault(mt, []).append(transform)
        target_mime_type = mimetypes.lookup(transform['mime-types']['target'])
        if target_mime_type:
            transform['mime-types']['target'] = target_mime_type['mime-type']
            output_ext = '.' + target_mime_type['extensions'][0]
            output_mime_type = target_mime_type['mime-type']
        else:
            output_ext = ''
            output_mime_type = transform['mime-types']['target']

        for example_id, example in enumerate(bblock.examples):
            snippets = example.get('snippets', ())
            for snippet_id, snippet in enumerate(snippets):
                found_mime_type = mimetypes.lookup(snippet.get('language'))
                mime_type = found_mime_type['mime-type'] if found_mime_type else snippet.get('language')

                if mime_type not in transform['mime-types']['source']:
                    continue

                output_fn = output_dir / f"example_{example_id + 1}_{snippet_id + 1}-{transform['type']}{output_ext}"

                ref = transform['ref'] if is_url(transform['ref']) else bblock.files_path / transform['ref']
                transform_metadata = TransformMetadata(type=transform['type'],
                                                       source_mime_type=mime_type,
                                                       target_mime_type=output_mime_type,
                                                       source_ref=ref,
                                                       transform_content=transform['code'],
                                                       metadata=transform.get('metadata'),
                                                       input_data=snippet['code'])
                try:
                    transform_result = transformers.transform(transform_metadata)
                    if transform_result:
                        with open(output_fn, 'w') as f:
                            f.write(transform_result)

                except Exception:
                    with open(output_fn.with_stem(output_fn.name + '.error'), 'w') as f:
                        f.write('Error generating transformed file:\n')
                        f.write(traceback.format_exc())
