"""Coverage for the x-bblocks-metadata provenance key added to annotated schemas."""
import json
from types import SimpleNamespace

import yaml

from ogc.bblocks.schema import add_bblocks_metadata, write_annotated_schema


def _bblock(**metadata):
    return SimpleNamespace(identifier='ogc.test.thing', metadata=metadata)


def _register(register_url=None):
    return SimpleNamespace(register_url=register_url)


def test_adds_all_fields():
    schema = {'type': 'object'}
    add_bblocks_metadata(schema, _bblock(version='1.2.0', dateOfLastChange='2026-09-14'),
                         _register('https://example.com/register.json'))
    assert schema['x-bblocks-metadata'] == {
        'itemIdentifier': 'ogc.test.thing',
        'register': 'https://example.com/register.json',
        'version': '1.2.0',
        'dateOfLastChange': '2026-09-14',
    }


def test_no_register_without_register_url():
    schema = {}
    add_bblocks_metadata(schema, _bblock(version='1.0', dateOfLastChange='2026-01-01'), _register())
    assert 'register' not in schema['x-bblocks-metadata']
    assert schema['x-bblocks-metadata']['itemIdentifier'] == 'ogc.test.thing'


def test_existing_keys_merged_with_generated_ones_overriding():
    schema = {'x-bblocks-metadata': {'itemIdentifier': 'bogus', 'custom': 'kept', 'register': 'https://old/'}}
    add_bblocks_metadata(schema, _bblock(version='2.0', dateOfLastChange='2026-02-02'),
                         _register('https://example.com/register.json'))
    assert schema['x-bblocks-metadata'] == {
        'itemIdentifier': 'ogc.test.thing',
        'custom': 'kept',
        'register': 'https://example.com/register.json',
        'version': '2.0',
        'dateOfLastChange': '2026-02-02',
    }


def test_stale_generated_keys_dropped_when_unknown():
    schema = {'x-bblocks-metadata': {'register': 'https://old/', 'version': '0.1'}}
    add_bblocks_metadata(schema, _bblock(), _register())
    assert schema['x-bblocks-metadata'] == {'itemIdentifier': 'ogc.test.thing'}


def test_non_dict_schema_untouched():
    schema = True
    add_bblocks_metadata(schema, _bblock(), _register())
    assert schema is True


def test_written_yaml_and_json_include_metadata(tmp_path):
    bblock = SimpleNamespace(identifier='ogc.test.thing', annotated_path=tmp_path,
                             metadata={'version': '1.0', 'dateOfLastChange': '2026-03-03'})
    register = SimpleNamespace(register_url='https://example.com/register.json', base_url=None,
                               local_bblock_files={}, imported_bblock_files={})
    write_annotated_schema(bblock, register, {'type': 'object'})
    expected = {
        'itemIdentifier': 'ogc.test.thing',
        'register': 'https://example.com/register.json',
        'version': '1.0',
        'dateOfLastChange': '2026-03-03',
    }
    assert yaml.safe_load((tmp_path / 'schema.yaml').read_text())['x-bblocks-metadata'] == expected
    assert json.loads((tmp_path / 'schema.json').read_text())['x-bblocks-metadata'] == expected
