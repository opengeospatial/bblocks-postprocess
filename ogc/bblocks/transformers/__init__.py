from __future__ import annotations

import functools
import importlib
from pathlib import Path
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata

transform_modules = []

for mod_file in Path(__file__).parent.glob('*.py'):
    if mod_file.name.startswith('_'):
        continue
    mod_name = f".{mod_file.stem}"
    module = importlib.import_module(mod_name, package=__name__)

    if hasattr(module, 'transform_type'):
        transform_modules.append(module)


@functools.lru_cache
def find_transformer(transform_type, source_mime_type, target_mime_type) -> module:
    for mod in transform_modules:
        if (mod.transform_type == transform_type
                and source_mime_type in mod.source_mime_types
                and target_mime_type in mod.target_mime_types):
            return mod


def transform(transform_metadata: TransformMetadata) -> AnyStr | None:
    transformer = find_transformer(transform_metadata.type,
                                   transform_metadata.source_mime_type,
                                   transform_metadata.target_mime_type)
    if not transformer:
        return None
    return transformer.transform(transform_metadata)
