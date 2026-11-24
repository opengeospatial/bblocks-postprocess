"""Tests for how ogc.bblocks.validation.json_.get_json_validator resolves $refs
(referencing registry): schemas of local, not-yet-published building blocks are served
from memory, remote schemas are fetched, and a property literally named "$id" does not
confuse resolution (the reason a patched jsonschema fork was used before; see
python-jsonschema#1085).
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from ogc.bblocks.validation.json_ import _fetch_remote, get_json_validator, validate_json
from ogc.bblocks.util import HTTP_TIMEOUT

LOCAL_URI = 'https://example.org/local/schema.yaml'


@pytest.fixture(autouse=True)
def _clear_remote_cache():
    _fetch_remote.cache_clear()
    yield
    _fetch_remote.cache_clear()


def _register(contents: str):
    bblock = SimpleNamespace(annotated_schema_contents=contents)
    return SimpleNamespace(local_bblock_files={LOCAL_URI: 'a.b'}, bblocks={'a.b': bblock})


def test_ref_to_local_bblock_is_served_from_contents():
    register = _register('type: integer\n')
    validator = get_json_validator({'$ref': LOCAL_URI}, None, register)
    assert validator.is_valid(1)
    assert not validator.is_valid('x')


def test_relative_ref_resolves_against_base_uri():
    register = _register('type: integer\n')
    schema = {'properties': {'n': {'$ref': 'schema.yaml'}}}
    validator = get_json_validator(schema, 'https://example.org/local/root.yaml', register)
    assert validator.is_valid({'n': 1})
    assert not validator.is_valid({'n': 'x'})


def test_ref_with_fragment_into_local_bblock():
    register = _register('$defs:\n  num:\n    type: number\n')
    validator = get_json_validator({'$ref': f'{LOCAL_URI}#/$defs/num'}, None, register)
    assert validator.is_valid(1.5)
    assert not validator.is_valid('x')


def test_remote_yaml_ref_is_fetched():
    response = mock.Mock(content=b'type: string\n')
    with mock.patch('ogc.bblocks.validation.json_.requests.get', return_value=response) as get:
        validator = get_json_validator({'$ref': 'https://example.org/remote.yaml'}, None, None)
        assert validator.is_valid('x')
        assert not validator.is_valid(1)
    get.assert_called_once_with('https://example.org/remote.yaml', timeout=HTTP_TIMEOUT)


def test_property_named_dollar_id():
    schema = {
        '$schema': 'http://json-schema.org/draft-07/schema#',
        '$id': 'https://example.org/with-id.json',
        'properties': {
            '$id': {'type': 'string'},
            'n': {'$ref': '#/definitions/num'},
        },
        'definitions': {'num': {'type': 'integer'}},
    }
    validator = get_json_validator(schema, None, None)
    assert validator.is_valid({'$id': 'x', 'n': 1})
    assert not validator.is_valid({'$id': 5, 'n': 1})
    assert not validator.is_valid({'n': 'x'})


def test_schema_without_dollar_schema_or_id_gets_base_uri():
    validator = get_json_validator({'properties': {'a': {'$ref': '#/$defs/s'}}, '$defs': {'s': {'type': 'string'}}},
                                   'file:///tmp/x.yaml', None)
    assert validator.is_valid({'a': 'x'})
    assert not validator.is_valid({'a': 1})


def test_unresolvable_ref_raises():
    validator = get_json_validator({'$ref': 'https://example.org/missing.json'}, None, _register(''))
    with mock.patch('ogc.bblocks.validation.json_.requests.get', side_effect=IOError('nope')):
        with pytest.raises(Exception):
            validate_json(1, validator)


def test_remote_ref_is_fetched_once_across_references_and_validators():
    url = 'https://example.org/remote.yaml'
    response = mock.Mock(content=b'type: string\n')
    schema = {'anyOf': [{'$ref': url}, {'$ref': url}], 'items': {'$ref': url}}
    with mock.patch('ogc.bblocks.validation.json_.requests.get', return_value=response) as get:
        for _ in range(2):
            validator = get_json_validator(schema, None, None)
            assert validator.is_valid('x')
            assert not validator.is_valid([1, 2])
    assert get.call_count == 1


def test_failed_remote_fetch_is_not_cached():
    ref = {'$ref': 'https://example.org/remote.yaml'}
    response = mock.Mock(content=b'type: string\n')
    with mock.patch('ogc.bblocks.validation.json_.requests.get', side_effect=[IOError('nope'), response]):
        with pytest.raises(Exception):
            validate_json('x', get_json_validator(ref, None, None))
        validate_json('x', get_json_validator(ref, None, None))
