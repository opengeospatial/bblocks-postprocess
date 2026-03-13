from __future__ import annotations

import importlib
import inspect
from pathlib import Path

from ogc.bblocks.models import Transformer

transformers: dict[str, Transformer] = {}


def _register_module(module) -> None:
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if issubclass(cls, Transformer) and cls is not Transformer:
            transformer = cls()
            for tt in transformer.transform_types:
                transformers[tt] = transformer


for _mod_file in Path(__file__).parent.glob('*.py'):
    if _mod_file.name.startswith('_'):
        continue
    _mod = importlib.import_module(f".{_mod_file.stem}", package=__name__)
    _register_module(_mod)
