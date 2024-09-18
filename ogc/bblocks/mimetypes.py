from __future__ import annotations

from pathlib import Path
from typing import Any

from ogc.na.util import load_yaml

_known_mimetypes = None


def get_known_mimetypes() -> dict[str, Any]:
    global _known_mimetypes
    if _known_mimetypes is None:
        _known_mimetypes = {k: {'id': k, 'defaultExtension': v['extensions'][0]} | v
                            for k, v in
                            load_yaml(Path(__file__).parent / 'known-mimetypes.yaml').items()}
    return _known_mimetypes


def from_extension(ext: str) -> dict | None:
    for entry in get_known_mimetypes().values():
        if ext in entry['extensions']:
            return entry


def lookup(t: str) -> dict | None:
    for k, entry in get_known_mimetypes().items():
        if t in (k, entry['mimeType']):
            return entry
        if 'aliases' in entry and entry['aliases'] and t in entry['aliases']:
            return entry


def normalize(t: str) -> str:
    n = lookup(t)
    if n:
        return n['mimeType']
    return t
