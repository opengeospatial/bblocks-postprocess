"""
Tests for ogc.bblocks.extension.Extender.process_extensions.

These build tiny real BuildingBlockRegister/BuildingBlock trees on disk (rather than
mocking Extender's internals) and drive the real process_extensions()/_process_schema()/
_process_openapi() code, per the project convention of verifying against real code
instead of a hand-rolled reimplementation.

Each bblock's "annotated" output (schema.yaml/openapi.yaml under annotated_path) is
written directly, standing in for what the annotate step would normally have produced -
Extender only cares about $ref structure, not annotation-specific content.
"""
import json

import pytest
import yaml

from ogc.bblocks.extension import Extender
from ogc.bblocks.models import BuildingBlockRegister


def _write_bblock(sources_dir, rel_path, *, name, item_class,
                   schema=None, openapi=None, extension_points=None):
    """
    Create a minimal bblock.json (+ schema.yaml and/or openapi.yaml, both as source
    and as its pre-baked "annotated" copy) under sources_dir/rel_path.
    """
    bblock_dir = sources_dir / rel_path
    bblock_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        'name': name,
        'status': 'stable',
        'dateTimeAddition': '2026-01-01T00:00:00Z',
        'itemClass': item_class,
        'version': '1.0.0',
    }
    if extension_points:
        metadata['extensionPoints'] = extension_points
    (bblock_dir / 'bblock.json').write_text(json.dumps(metadata))

    if schema is not None:
        (bblock_dir / 'schema.yaml').write_text(yaml.safe_dump(schema))
    if openapi is not None:
        (bblock_dir / 'openapi.yaml').write_text(yaml.safe_dump(openapi))

    return bblock_dir


def _write_annotated(annotated_path, rel_path, *, schema=None, openapi=None):
    out_dir = annotated_path / rel_path
    out_dir.mkdir(parents=True, exist_ok=True)
    if schema is not None:
        (out_dir / 'schema.yaml').write_text(yaml.safe_dump(schema))
    if openapi is not None:
        (out_dir / 'openapi.yaml').write_text(yaml.safe_dump(openapi))


def _build_register(tmp_path, base_url=None, *, monkeypatch=None):
    # Real invocations always run with cwd = the register's own root, and base_url
    # resolution (models.py/schema.py) relativizes paths against cwd - so match that
    # here rather than leaving cwd at the repo checkout.
    if monkeypatch is not None:
        monkeypatch.chdir(tmp_path)
    sources_dir = tmp_path / '_sources'
    sources_dir.mkdir()
    annotated_path = tmp_path / 'annotated'
    return sources_dir, annotated_path, base_url


class TestJsonSchemaExtension:
    """
    Sanity/regression coverage for the plain JSON Schema extensionPoints path
    (Extender._process_schema), which is unaffected by the OpenAPI bug below.
    """

    def _make_fixture(self, tmp_path, base_url, monkeypatch):
        sources_dir, annotated_path, base_url = _build_register(tmp_path, base_url, monkeypatch=monkeypatch)

        base_schema = {
            'type': 'object',
            'properties': {
                'value': {'type': 'string'},
            },
        }
        target_schema = {
            'type': 'object',
            'properties': {
                'value': {'type': 'integer'},
            },
        }
        _write_bblock(sources_dir, 'base', name='Base', item_class='schema', schema=base_schema)
        _write_annotated(annotated_path, 'base', schema=base_schema)

        _write_bblock(sources_dir, 'target', name='Target', item_class='schema', schema=target_schema)
        _write_annotated(annotated_path, 'target', schema=target_schema)

        child_schema = {'type': 'object'}
        _write_bblock(
            sources_dir, 'child', name='Child', item_class='schema', schema=child_schema,
            extension_points={
                'baseBuildingBlock': 'test.base',
                'extensions': {'test.base': 'test.target'},
            },
        )

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path,
                                         prefix='test.', base_url=base_url)
        return register

    def test_process_extensions_with_base_url(self, tmp_path, monkeypatch):
        register = self._make_fixture(tmp_path, base_url='https://example.org/', monkeypatch=monkeypatch)
        extender = Extender(register)
        child = register.bblocks['test.child']

        result, is_openapi = extender.process_extensions(child)

        assert is_openapi is False
        # The root wrap's own $ref is always relativized against the child's own
        # annotated_path (base_url only affects extension_target_ref substitution
        # values, not this top-level wrap) - see _process_schema.
        assert result['allOf'][0]['$ref'] == '../base/schema.yaml'
        assert result['x-bblocks-extends'] == 'test.base'

    def test_process_extensions_without_base_url(self, tmp_path, monkeypatch):
        register = self._make_fixture(tmp_path, base_url=None, monkeypatch=monkeypatch)
        extender = Extender(register)
        child = register.bblocks['test.child']

        result, is_openapi = extender.process_extensions(child)

        assert is_openapi is False
        # Relative to the child's own annotated_path, per _process_schema.
        assert result['allOf'][0]['$ref'] == '../base/schema.yaml'


class TestOpenApiExtension:
    """
    Coverage for Extender._process_openapi, including the additive-paths merge and
    the known base_url-unset bug around merged-in additions content (see
    _substitute_openapi_schemas / resolve_schema_reference).
    """

    def _make_fixture(self, tmp_path, base_url, monkeypatch, *, base_rel_path='base'):
        sources_dir, annotated_path, base_url = _build_register(tmp_path, base_url, monkeypatch=monkeypatch)

        base_openapi = {
            'openapi': '3.1.0',
            'info': {'title': 'Base API', 'version': '1.0.0'},
            'paths': {
                '/items': {
                    'get': {
                        'responses': {
                            '200': {'description': 'OK'},
                        },
                    },
                },
            },
        }
        _write_bblock(sources_dir, base_rel_path, name='Base API', item_class='api', openapi=base_openapi)
        _write_annotated(annotated_path, base_rel_path, openapi=base_openapi)

        source_schema = {'type': 'object', 'properties': {'x': {'type': 'string'}}}
        _write_bblock(sources_dir, 'source', name='Extension source', item_class='schema', schema=source_schema)
        _write_annotated(annotated_path, 'source', schema=source_schema)

        target_schema = {'type': 'object', 'properties': {'x': {'type': 'integer'}}}
        _write_bblock(sources_dir, 'target', name='Extension target', item_class='schema', schema=target_schema)
        _write_annotated(annotated_path, 'target', schema=target_schema)

        # Additive path whose response schema references the declared extension source
        # via bblocks:// - resolved (by resolve_all_schema_references, ahead of the
        # merge) into a path relative to the *child's own* annotated_path, then
        # (re-)resolved again during the substitution walk using the *base*'s location
        # as from_schema (_substitute_openapi_schemas) - see extension_target_ref in
        # process_extensions().
        child_openapi_additions = {
            'paths': {
                '/extra': {
                    'get': {
                        'responses': {
                            '200': {
                                'description': 'OK',
                                'content': {
                                    'application/json': {
                                        'schema': {'$ref': 'bblocks://test.source'},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        _write_bblock(
            sources_dir, 'child', name='Child API', item_class='api', openapi=child_openapi_additions,
            extension_points={
                'baseBuildingBlock': f'test.{base_rel_path.replace("/", ".")}',
                'extensions': {'test.source': 'test.target'},
            },
        )

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path,
                                         prefix='test.', base_url=base_url)
        return register

    def test_additive_path_with_base_url(self, tmp_path, monkeypatch):
        """
        With base_url set, refs resolved ahead of the merge are absolute URLs, so they
        stay correct no matter where in the document they end up - no bug here.
        """
        register = self._make_fixture(tmp_path, base_url='https://example.org/', monkeypatch=monkeypatch)
        extender = Extender(register)
        child = register.bblocks['test.child']

        document, is_openapi = extender.process_extensions(child)

        assert is_openapi is True
        # Base path untouched
        assert '/items' in document['paths']
        # New path merged in, with the source ref substituted for the target - both
        # sides resolved to absolute (thus context-independent) URLs
        extra_schema = document['paths']['/extra']['get']['responses']['200']['content'][
            'application/json']['schema']
        assert extra_schema['allOf'][0]['$ref'] == 'https://example.org/annotated/source/schema.yaml'
        assert extra_schema['allOf'][1]['$ref'] == 'https://example.org/annotated/target/schema.yaml'
        assert extra_schema['allOf'][1]['x-bblocks-extension-source'] == 'test.source'
        assert extra_schema['allOf'][1]['x-bblocks-extension-target'] == 'test.target'

    def test_additive_path_without_base_url_same_depth_masks_bug(self, tmp_path, monkeypatch):
        """
        When the base and the extending bblock happen to sit at the same directory
        depth, the (buggy) wrong from_schema anchor used for the whole merged document
        still happens to produce the right relative path - the bug is latent, not
        visibly broken, in this common case.
        """
        register = self._make_fixture(tmp_path, base_url=None, monkeypatch=monkeypatch, base_rel_path='base')
        extender = Extender(register)
        child = register.bblocks['test.child']

        document, is_openapi = extender.process_extensions(child)

        extra_schema = document['paths']['/extra']['get']['responses']['200']['content'][
            'application/json']['schema']
        assert extra_schema['allOf'][0]['$ref'] == '../source/schema.yaml'
        assert extra_schema['allOf'][1]['$ref'] == '../target/schema.yaml'

    def test_additive_path_without_base_url_different_depth_reproduces_bug(self, tmp_path, monkeypatch):
        """
        Reproduces the known latent bug (see project memory
        project_openapi_extension_points): with base_url unset, additive-path content
        merged in from the extending bblock's own openapi.yaml has its $refs resolved
        by resolve_all_schema_references relative to the *extending* bblock's own
        annotated_path, but the merged document is then walked/re-resolved as a whole
        using the *base* bblock's location as from_schema
        (_process_openapi -> _substitute_openapi_schemas). When the base and the
        extending bblock live at different directory depths, that anchor mismatch
        makes the "already correct" relative ref resolve to the wrong file.

        This test documents the current (buggy) behavior rather than a fix: it should
        succeed, producing the same '../source/schema.yaml' / '../target/schema.yaml'
        substitution as the same-depth case above, but today raises instead.
        """
        # Base one level deeper than source/target/child, so a ref that's correct
        # relative to child's directory is *not* correct relative to base's directory.
        register = self._make_fixture(tmp_path, base_url=None, monkeypatch=monkeypatch, base_rel_path='api/base')
        extender = Extender(register)
        child = register.bblocks['test.child']

        with pytest.raises(OSError, match='source/schema.yaml'):
            # Today this raises (file not found under the wrong, base-relative anchor)
            # instead of resolving '../source/schema.yaml' correctly relative to
            # child's own directory, as the same-depth case does.
            extender.process_extensions(child)

    def test_pure_additive_extension_points_without_extensions_key(self, tmp_path, monkeypatch):
        """
        extensionPoints.extensions is optional (bblock.schema.yaml) - a bblock can
        declare extensionPoints purely to pull in the base document and merge its own
        additive paths, with no substitutions at all.
        """
        sources_dir, annotated_path, base_url = _build_register(tmp_path, base_url=None, monkeypatch=monkeypatch)

        base_openapi = {
            'openapi': '3.1.0',
            'info': {'title': 'Base API', 'version': '1.0.0'},
            'paths': {
                '/items': {
                    'get': {'responses': {'200': {'description': 'OK'}}},
                },
            },
        }
        _write_bblock(sources_dir, 'base', name='Base API', item_class='api', openapi=base_openapi)
        _write_annotated(annotated_path, 'base', openapi=base_openapi)

        child_openapi_additions = {
            'paths': {
                '/local': {
                    'get': {'responses': {'200': {'description': 'OK'}}},
                },
            },
        }
        _write_bblock(
            sources_dir, 'child', name='Child API', item_class='api', openapi=child_openapi_additions,
            extension_points={'baseBuildingBlock': 'test.base'},
        )

        register = BuildingBlockRegister(sources_dir, annotated_path=annotated_path,
                                         prefix='test.', base_url=base_url)
        extender = Extender(register)
        child = register.bblocks['test.child']

        document, is_openapi = extender.process_extensions(child)

        assert is_openapi is True
        assert set(document['paths'].keys()) == {'/items', '/local'}
