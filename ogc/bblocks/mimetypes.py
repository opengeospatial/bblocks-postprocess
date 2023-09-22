from __future__ import annotations

from pathlib import Path

from ogc.na.util import load_yaml

KNOWN_MIMETYPES = load_yaml(Path(__file__).parent / 'known-mimetypes.yaml')


def from_extension(ext: str) -> dict | None:
    for entry in KNOWN_MIMETYPES.values():
        if ext in entry['extensions']:
            return entry


def lookup(t: str) -> dict | None:
    for k, entry in KNOWN_MIMETYPES.items():
        if t in (k, entry['mime-type']):
            return entry
        if 'aliases' in entry and entry['aliases'] and t in entry['aliases']:
            return entry

