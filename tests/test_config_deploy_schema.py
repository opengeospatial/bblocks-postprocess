"""Tests for the "deploy" section of bblocks-config.yaml (read by validate-and-process.yml)."""
import jsonschema
import pytest

from ogc.bblocks.util import get_schema


@pytest.mark.parametrize('deploy', [
    {},
    {'mode': 'artifact'},
    {'mode': 'branch'},
    {'mode': 'branch', 'branch': 'pages'},
    {'branch': 'gh-pages'},
])
def test_deploy_accepted(deploy):
    jsonschema.validate({'deploy': deploy}, get_schema('bblocks-config'))


@pytest.mark.parametrize('deploy', [
    {'mode': 'pages'},
    {'mode': 'actions'},
    {'branch': ''},
    {'branch': 123},
    {'mode': 'branch', 'unknown': True},
])
def test_deploy_rejected(deploy):
    with pytest.raises(jsonschema.exceptions.ValidationError):
        jsonschema.validate({'deploy': deploy}, get_schema('bblocks-config'))
