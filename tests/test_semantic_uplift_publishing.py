"""
Regression coverage for BuildingBlock.published_semantic_uplift and
BuildingBlockRegister.get_inherited_post_uplift_steps.

published_semantic_uplift must publish a bblock's *complete* set of semantic-uplift
additionalSteps (all stages, inheritable or not) - it is the value written into both
register.json's per-block entry and json-full's per-block dump, and both must describe
this bblock's own full semantic uplift, not just the fragment other bblocks may inherit
from it. Filtering to "inheritable" steps only happens in
get_inherited_post_uplift_steps, when a *dependent* bblock collects what it inherits.

See docs/inheritable-post-uplift-steps.md and models.py's
BuildingBlock.published_semantic_uplift / BuildingBlockRegister.get_inherited_post_uplift_steps.

Per project convention, this builds a tiny real BuildingBlockRegister/BuildingBlock tree
on disk and drives the real properties/methods, rather than mocking them.
"""
import json

import yaml

from ogc.bblocks.models import BuildingBlockRegister


def _write_bblock(sources_dir, rel_path, *, name, depends_on=None, inherited_post_steps=None,
                   semantic_uplift=None):
    bblock_dir = sources_dir / rel_path
    bblock_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        'name': name,
        'status': 'stable',
        'dateTimeAddition': '2026-01-01T00:00:00Z',
        'itemClass': 'schema',
        'version': '1.0.0',
    }
    if depends_on:
        metadata['dependsOn'] = depends_on
    (bblock_dir / 'bblock.json').write_text(json.dumps(metadata))
    (bblock_dir / 'schema.yaml').write_text(yaml.safe_dump({'type': 'object'}))

    su = dict(semantic_uplift or {})
    if inherited_post_steps is not None:
        su['inheritedPostSteps'] = inherited_post_steps
    if su:
        (bblock_dir / 'semantic-uplift.yaml').write_text(yaml.safe_dump(su))

    return bblock_dir


def _build_register(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / '_sources'
    sources_dir.mkdir()
    annotated_path = tmp_path / 'annotated'
    return sources_dir, annotated_path


class TestPublishedSemanticUplift:

    def test_publishes_full_step_set_unfiltered(self, tmp_path, monkeypatch):
        """All steps are published - pre-stage, non-inheritable post-stage, and
        inheritable post-stage alike - not just the inheritable subset."""
        sources_dir, annotated_path = _build_register(tmp_path, monkeypatch)

        _write_bblock(
            sources_dir, 'base', name='Base',
            semantic_uplift={
                'additionalSteps': [
                    {'type': 'jq', 'code': '.'},
                    {'type': 'shacl', 'code': 'shape rules', 'inheritable': False},
                    {'type': 'sparql-construct', 'code': 'construct query', 'inheritable': True},
                ],
            },
        )

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path, prefix='test.')
        base = register.bblocks['test.base']

        published = base.published_semantic_uplift['additionalSteps']
        assert len(published) == 3
        assert [s['type'] for s in published] == ['jq', 'shacl', 'sparql-construct']
        assert published[1]['inheritable'] is False
        assert published[2]['inheritable'] is True

    def test_resolves_ref_into_code(self, tmp_path, monkeypatch):
        """ref-based steps are still inlined into code for portability, regardless of
        whether they end up being inheritable."""
        sources_dir, annotated_path = _build_register(tmp_path, monkeypatch)

        base_dir = _write_bblock(
            sources_dir, 'base', name='Base',
            semantic_uplift={
                'additionalSteps': [
                    {'type': 'jq', 'ref': 'transform.jq'},
                ],
            },
        )
        (base_dir / 'transform.jq').write_text('.foo')

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path, prefix='test.')
        base = register.bblocks['test.base']

        published = base.published_semantic_uplift['additionalSteps']
        assert published[0]['code'] == '.foo'
        assert 'ref' not in published[0]


class TestInheritedPostUpliftSteps:

    def test_only_inheritable_post_steps_are_inherited(self, tmp_path, monkeypatch):
        """A dependent that opts in to inheritedPostSteps gets only the dependency's
        inheritable post-stage steps - not its jq step, and not its non-inheritable
        post-stage step."""
        sources_dir, annotated_path = _build_register(tmp_path, monkeypatch)

        _write_bblock(
            sources_dir, 'base', name='Base',
            semantic_uplift={
                'additionalSteps': [
                    {'type': 'jq', 'code': '.'},
                    {'type': 'shacl', 'code': 'non-inherited shape', 'inheritable': False},
                    {'type': 'sparql-construct', 'code': 'inherited construct', 'inheritable': True},
                ],
            },
        )
        _write_bblock(
            sources_dir, 'child', name='Child', depends_on=['test.base'],
            inherited_post_steps=True,
        )

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path, prefix='test.')

        inherited = register.get_inherited_post_uplift_steps('test.child')
        assert len(inherited) == 1
        assert inherited[0]['type'] == 'sparql-construct'
        assert inherited[0]['code'] == 'inherited construct'
        assert inherited[0]['_source_bblock'] == 'test.base'

    def test_no_inheritance_without_opt_in(self, tmp_path, monkeypatch):
        sources_dir, annotated_path = _build_register(tmp_path, monkeypatch)

        _write_bblock(
            sources_dir, 'base', name='Base',
            semantic_uplift={
                'additionalSteps': [
                    {'type': 'shacl', 'code': 'inherited shape', 'inheritable': True},
                ],
            },
        )
        _write_bblock(sources_dir, 'child', name='Child', depends_on=['test.base'])

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path, prefix='test.')

        assert register.get_inherited_post_uplift_steps('test.child') == []
