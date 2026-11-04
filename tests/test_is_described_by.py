"""isDescribedBy: a list-or-string, dependency-like relation (like isProfileOf)."""
import json
import logging

import yaml

from ogc.bblocks.models import BuildingBlockRegister, get_direct_dependencies


def _write(sources_dir, name, **extra):
    d = sources_dir / name
    d.mkdir(parents=True)
    metadata = {
        'name': name,
        'status': 'stable',
        'dateTimeAddition': '2026-01-01T00:00:00Z',
        'itemClass': 'schema',
        'version': '1.0.0',
        **extra,
    }
    (d / 'bblock.json').write_text(json.dumps(metadata))
    (d / 'schema.yaml').write_text(yaml.safe_dump({'type': 'object'}))


def _register(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return BuildingBlockRegister(tmp_path / '_sources', annotated_path=tmp_path / 'annotated', prefix='test.')


def test_get_direct_dependencies_covers_all_fields():
    md = {'dependsOn': ['a', 'b'], 'isProfileOf': 'c', 'isDescribedBy': ['b', 'd']}
    assert get_direct_dependencies(md) == ['a', 'b', 'c', 'd']
    assert get_direct_dependencies({'profileOf': ['x']}) == ['x']
    assert get_direct_dependencies({}) == []


def test_string_and_bblocks_uri_normalized_to_list(tmp_path, monkeypatch):
    sources = tmp_path / '_sources'
    sources.mkdir()
    _write(sources, 'model')
    _write(sources, 'schema', isDescribedBy='bblocks://test.model')
    register = _register(tmp_path, monkeypatch)
    assert register.bblocks['test.schema'].metadata['isDescribedBy'] == ['test.model']


def test_depends_on_is_union_and_walked(tmp_path, monkeypatch):
    sources = tmp_path / '_sources'
    sources.mkdir()
    _write(sources, 'model')
    _write(sources, 'other')
    _write(sources, 'schema', isDescribedBy=['test.model'], dependsOn=['test.model', 'test.other'])
    register = _register(tmp_path, monkeypatch)
    md = register.bblocks['test.schema'].metadata
    assert md['isDescribedBy'] == ['test.model']
    assert md['dependsOn'] == ['test.model', 'test.other']
    assert [d.identifier for d in register.find_dependencies('test.schema')] == [
        'test.schema', 'test.model', 'test.other']
    # model is processed before the block it describes
    order = list(register.bblocks)
    assert order.index('test.model') < order.index('test.schema')


def test_cycle_through_is_described_by_is_warned(tmp_path, monkeypatch, caplog):
    sources = tmp_path / '_sources'
    sources.mkdir()
    _write(sources, 'model', isProfileOf='test.schema')
    _write(sources, 'schema', isDescribedBy=['test.model'])
    with caplog.at_level(logging.WARNING):
        _register(tmp_path, monkeypatch)
    assert any('Circular dependencies' in r.message for r in caplog.records)


def test_depends_on_includes_profile_and_described_by_targets(tmp_path, monkeypatch):
    sources = tmp_path / '_sources'
    sources.mkdir()
    _write(sources, 'base')
    _write(sources, 'model')
    _write(sources, 'schema', isProfileOf='test.base', isDescribedBy='test.model')
    register = _register(tmp_path, monkeypatch)
    md = register.bblocks['test.schema'].metadata
    assert md['dependsOn'] == ['test.base', 'test.model']
    assert md['isProfileOf'] == ['test.base']
    assert md['isDescribedBy'] == ['test.model']
