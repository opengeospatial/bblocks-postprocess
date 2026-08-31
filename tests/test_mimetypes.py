"""Smoke test for the test scaffolding itself, plus a first real Tier-1 case
(see docs/pytest-testing-plan.md) for ogc.bblocks.mimetypes.
"""
from ogc.bblocks.mimetypes import from_extension, lookup, normalize


def test_from_extension_known():
    entry = from_extension('json')
    assert entry is not None
    assert entry['mimeType'] == 'application/json'


def test_from_extension_unknown():
    assert from_extension('not-a-real-extension') is None


def test_lookup_by_alias():
    entry = lookup('turtle')
    assert entry is not None
    assert entry['mimeType'] == 'text/turtle'


def test_normalize_passes_through_unknown_type():
    assert normalize('application/x-totally-unknown') == 'application/x-totally-unknown'


def test_normalize_known_alias():
    assert normalize('turtle') == 'text/turtle'
