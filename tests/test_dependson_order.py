"""Guards against issue #91: bblock.metadata['dependsOn'] was built from a set, so its order
(and the topological order derived from it) varied with PYTHONHASHSEED."""
import json

import yaml

from ogc.bblocks.models import BuildingBlockRegister


def _write(sources_dir, name, depends_on=None):
    d = sources_dir / name
    d.mkdir(parents=True)
    metadata = {
        'name': name,
        'status': 'stable',
        'dateTimeAddition': '2026-01-01T00:00:00Z',
        'itemClass': 'schema',
        'version': '1.0.0',
    }
    if depends_on:
        metadata['dependsOn'] = depends_on
    (d / 'bblock.json').write_text(json.dumps(metadata))
    (d / 'schema.yaml').write_text(yaml.safe_dump({'type': 'object'}))


def test_depends_on_is_sorted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / '_sources'
    sources_dir.mkdir()
    names = ['zeta', 'alpha', 'mid', 'beta', 'omega', 'gamma']
    for n in names:
        _write(sources_dir, n)
    _write(sources_dir, 'top', depends_on=[f'test.{n}' for n in names])

    register = BuildingBlockRegister(sources_dir, annotated_path=tmp_path / 'annotated', prefix='test.')

    deps = register.bblocks['test.top'].metadata['dependsOn']
    assert deps == sorted(f'test.{n}' for n in names)


def test_canonical_cycles_ignore_start_node_and_order():
    import networkx as nx
    from ogc.bblocks.models import _canonical_cycles

    g = nx.DiGraph([('b', 'a'), ('a', 'b'), ('d', 'c'), ('c', 'e'), ('e', 'd')])
    assert _canonical_cycles(g) == [['a', 'b'], ['c', 'e', 'd']]
