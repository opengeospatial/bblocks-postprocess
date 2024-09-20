from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from ogc.bblocks.models import Transformer

transformers: dict[str, Transformer] = {}

for mod_file in Path(__file__).parent.glob('*.py'):
    if mod_file.name.startswith('_'):
        continue
    mod_name = f".{mod_file.stem}"
    module = importlib.import_module(mod_name, package=__name__)

    for cls_name, cls in inspect.getmembers(module, inspect.isclass):
        if issubclass(cls, Transformer) and cls is not Transformer:
            # noinspection PyArgumentList
            transformer = cls()
            for tt in transformer.transform_types:
                    transformers[tt] = transformer
