"""Tests for the "license" field accepting a plain string (shorthand for {name: <string>},
e.g. an SPDX identifier like "Apache-2.0") in both bblock.json and bblocks-config.yaml, in
addition to the {name, url} object form. See ogc.bblocks.util.normalize_license.
"""
import jsonschema
import pytest

from ogc.bblocks.util import get_schema, normalize_license

MINIMAL_BBLOCK = {
    'name': 'Test Building Block',
    'status': 'stable',
    'dateTimeAddition': '2026-01-01T00:00:00Z',
    'itemClass': 'schema',
    'version': '1.0.0',
}


@pytest.mark.parametrize('license_value, expected', [
    ('Apache-2.0', {'name': 'Apache-2.0'}),
    ({'name': 'CC BY 4.0'}, {'name': 'CC BY 4.0'}),
    ({'url': 'https://example.org/LICENSE'}, {'url': 'https://example.org/LICENSE'}),
    (None, None),
])
def test_normalize_license(license_value, expected):
    assert normalize_license(license_value) == expected


@pytest.mark.parametrize('license_value', [
    'Apache-2.0',
    {'name': 'CC BY 4.0'},
    {'url': 'https://example.org/LICENSE'},
    {'name': 'CC BY 4.0', 'url': 'https://example.org/LICENSE'},
])
def test_bblock_schema_accepts_license(license_value):
    jsonschema.validate({**MINIMAL_BBLOCK, 'license': license_value}, get_schema('bblock'))


@pytest.mark.parametrize('license_value', [
    123,
    {},
    {'other': 'field'},
])
def test_bblock_schema_rejects_invalid_license(license_value):
    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate({**MINIMAL_BBLOCK, 'license': license_value}, get_schema('bblock'))


@pytest.mark.parametrize('license_value', [
    'Apache-2.0',
    {'name': 'CC BY 4.0'},
    {'url': 'https://example.org/LICENSE'},
])
def test_bblocks_config_schema_accepts_license(license_value):
    jsonschema.validate({'license': license_value}, get_schema('bblocks-config'))


@pytest.mark.parametrize('license_value', [
    123,
    {},
])
def test_bblocks_config_schema_rejects_invalid_license(license_value):
    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate({'license': license_value}, get_schema('bblocks-config'))
