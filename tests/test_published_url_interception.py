"""A -> B -> A reference chains: a local building block (register A) that is also
published, and an imported building block of register B whose schema refers to the
*published* URL of A's schema. While building A, that URL has to resolve to the
working copy (annotated schema in the build dir, not yet published), not to the stale
published one.
"""
import json
from types import SimpleNamespace
from unittest import mock

import yaml

from ogc.bblocks.models import BuildingBlockRegister
from ogc.bblocks.validation.json_ import get_json_validator, validate_json

PUBLISHED_A = 'https://example.org/a/build/annotated/test/a/schema.yaml'
PUBLISHED_A_JSON = 'https://example.org/a/build/annotated/test/a/schema.json'
PUBLISHED_A_SOURCE = 'https://example.org/a/_sources/a/schema.yaml'
PUBLISHED_B = 'https://example.org/b/build/annotated/test/b/schema.yaml'


def _make_register(tmp_path, monkeypatch, base_url=None):
    monkeypatch.chdir(tmp_path)
    sources = tmp_path / '_sources'
    (sources / 'a').mkdir(parents=True)
    (sources / 'a' / 'bblock.json').write_text(json.dumps({
        'name': 'a', 'status': 'stable', 'dateTimeAddition': '2026-01-01T00:00:00Z',
        'itemClass': 'schema', 'version': '1.0.0',
    }))
    # Working copy: an integer. The published (stale) one is a string.
    (sources / 'a' / 'schema.yaml').write_text(yaml.safe_dump({'type': 'integer'}))
    annotated = tmp_path / 'annotated'
    (annotated / 'a').mkdir(parents=True)
    (annotated / 'a' / 'schema.yaml').write_text(yaml.safe_dump({'type': 'integer'}))

    imported = SimpleNamespace(bblocks={
        # Previously published version of the local block, pulled in through B's imports
        'test.a': {
            'itemIdentifier': 'test.a',
            'schema': {'application/yaml': PUBLISHED_A, 'application/json': PUBLISHED_A_JSON},
            'sourceSchema': PUBLISHED_A_SOURCE,
        },
        'test.b': {
            'itemIdentifier': 'test.b',
            'schema': {'application/yaml': PUBLISHED_B},
        },
    })
    return BuildingBlockRegister(sources, annotated_path=annotated, prefix='test.',
                                 imported_bblocks=imported, base_url=base_url)


def test_published_urls_of_local_bblocks_map_to_working_copy(tmp_path, monkeypatch):
    register = _make_register(tmp_path, monkeypatch, base_url='http://localhost:9090/register/')
    for url in (PUBLISHED_A, PUBLISHED_A_JSON, PUBLISHED_A_SOURCE):
        assert register.local_bblock_files.get(url) == 'test.a'
    # Not-local bblocks are still only reachable as imported
    assert PUBLISHED_B not in register.local_bblock_files
    assert register.imported_bblock_files[PUBLISHED_B] == 'test.b'


def test_chain_a_b_a_validates_against_working_copy(tmp_path, monkeypatch):
    register = _make_register(tmp_path, monkeypatch, base_url='http://localhost:9090/register/')
    # B's (remote) schema refers to A's published URL
    b_schema = yaml.safe_dump({'$ref': PUBLISHED_A})
    response = mock.Mock(content=b_schema.encode())

    def fake_get(url, *args, **kwargs):
        if url == PUBLISHED_B:
            return response
        raise AssertionError(f'Stale published schema fetched: {url}')

    with mock.patch('ogc.bblocks.validation.json_.requests.get', side_effect=fake_get):
        validator = get_json_validator({'$ref': PUBLISHED_B}, None, register)
        assert validator.is_valid(1)
        assert not validator.is_valid('x')
