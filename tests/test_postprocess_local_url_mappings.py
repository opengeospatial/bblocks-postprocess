"""register.json gets a `localUrlMappings` key (for the local viewer) iff local URL mappings
are in effect, i.e. bblocks-config-local.yml was present. Drives the real postprocess() on an
empty register."""
import json

import pytest

from ogc.bblocks.postprocess import postprocess


def _run(tmp_path, monkeypatch, **kwargs):
    monkeypatch.chdir(tmp_path)
    items = tmp_path / '_sources'
    items.mkdir()
    register_file = tmp_path / 'build' / 'register.json'
    register_file.parent.mkdir()
    postprocess(registered_items_path=items,
                output_file=register_file,
                base_url='http://localhost:9090/register/',
                annotated_path=tmp_path / 'annotated',
                test_outputs_path=tmp_path / 'build' / 'tests',
                imported_registers=[],
                additional_metadata={},
                skip_permissions=True,
                **kwargs)
    return json.loads(register_file.read_text())


def test_local_url_mappings_published_in_register(tmp_path, monkeypatch):
    register = _run(tmp_path, monkeypatch,
                    local_url_mappings={'https://example.com/up': '/upstream/build'})
    assert register['localUrlMappings'] == {'https://example.com/up': '/upstream/build'}


@pytest.mark.parametrize('mappings', [None, {}])
def test_no_local_url_mappings_key_without_mappings(tmp_path, monkeypatch, mappings):
    register = _run(tmp_path, monkeypatch, local_url_mappings=mappings)
    assert 'localUrlMappings' not in register
