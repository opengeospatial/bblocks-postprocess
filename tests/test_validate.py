"""Regression test for ogc.bblocks.validate._mime_type_for_extension.

Guards against a bug where validate_transform_output() / validate_test_resources()
passed the raw dict returned by ogc.bblocks.mimetypes.from_extension() straight
through as `file_format`, instead of unwrapping its 'mimeType' string. That dict
eventually reached NATIVE_RDF_LANGS.get(file_format, ...) in
ogc.bblocks.validation.rdf.RdfValidator._load_graph, which raised
"TypeError: unhashable type: 'dict'" for any RDF-suffixed transform output
(.ttl/.jsonld/.rdf) -- see the reported traceback in validate_transform_output.
"""
import pytest

from ogc.bblocks.validate import _mime_type_for_extension, _resource_not_accessible_item
from ogc.bblocks.validation import ValidationItemSource, ValidationItemSourceType, ValidationReportSection


def test_mime_type_for_extension_returns_string_not_dict():
    mime_type = _mime_type_for_extension('.ttl')
    assert mime_type == 'text/turtle'
    assert isinstance(mime_type, str)


def test_mime_type_for_extension_accepts_suffix_with_or_without_dot():
    assert _mime_type_for_extension('.ttl') == _mime_type_for_extension('ttl')


def test_mime_type_for_extension_unknown_suffix():
    assert _mime_type_for_extension('.not-a-real-extension') is None


def test_mime_type_for_extension_empty_suffix():
    assert _mime_type_for_extension('') is None


def test_resource_not_accessible_item_is_a_failed_report_with_message():
    """Regression test for issue #48: a broken example/test ref should surface as a
    scoped validation failure (not an unhandled exception that kills the whole run)."""
    source = ValidationItemSource(
        type=ValidationItemSourceType.EXAMPLE,
        example_index=1,
        snippet_index=1,
    )

    item = _resource_not_accessible_item(source, ref='missing.json', load_error='404 Not Found')

    assert item.failed
    entries = item.sections[ValidationReportSection.FILES]
    assert len(entries) == 1
    assert entries[0].is_error
    assert 'missing.json' in entries[0].message
    assert '404 Not Found' in entries[0].message


def test_validate_transform_output_passes_snippet_closures_to_validators(mocker, tmp_path):
    from ogc.bblocks.validate import validate_transform_output

    calls = []

    class FakeValidator:
        def __init__(self, *args, **kwargs):
            pass

        def validate(self, *args, **kwargs):
            calls.append(kwargs.get('additional_shacl_closures'))

    mocker.patch('ogc.bblocks.validate.JsonValidator', FakeValidator)
    mocker.patch('ogc.bblocks.validate.RdfValidator', FakeValidator)
    profile = mocker.MagicMock()
    output_file = tmp_path / 'out.json'
    output_file.write_text('{}')

    validate_transform_output(profile, mocker.MagicMock(), 't', output_file, tmp_path / 'report.json',
                              additional_shacl_closures=['https://example.com/closure.ttl'])

    assert calls == [['https://example.com/closure.ttl']] * 2

    
def test_uplift_does_not_fetch_a_reference_to_the_bblocks_own_context(mocker, tmp_path):
    import json
    from ogc.bblocks.validation import ValidationReportItem
    from ogc.bblocks.validation.rdf import RdfValidator

    own_url = 'https://example.com/register/annotated/thing/context.jsonld'
    validator = RdfValidator.__new__(RdfValidator)
    validator.bblock = mocker.MagicMock()
    validator.bblock.metadata = {'ldContext': own_url}
    validator.jsonld_error = None
    validator.jsonld_url = own_url
    validator.jsonld_context = {'@context': {'name': 'https://schema.org/name'}}
    validator.uplifter = mocker.MagicMock()
    validator.uplifter.pre_uplift.side_effect = lambda report, doc: doc
    validator.uplifter.post_uplift.side_effect = lambda report, graph: graph

    doc = tmp_path / 'doc.json'
    doc.write_text(json.dumps({'@context': [own_url, {'ex': 'https://example.com/ns/'}],
                               '@id': 'ex:a', 'name': 'A', 'ex:p': 'B'}))
    report = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.EXAMPLE, filename=doc))

    graph = validator._load_graph(doc, tmp_path / 'out.json', report)

    # own_url is not resolvable: it must not have been fetched
    assert len(graph) == 2
    uplifted = json.loads((tmp_path / 'out.jsonld').read_text())
    assert uplifted['@context'] == [own_url, {'ex': 'https://example.com/ns/'}]


def test_validate_transform_output_does_not_publish_profile_shacl_shapes(mocker, tmp_path):
    # opengeospatial/bblocks-postprocess#100: the profile bblock may already have been processed,
    # with absolute shaclShapes in its published metadata; validating a transform output against it
    # must not overwrite them with working-directory-relative paths.
    from ogc.bblocks.validate import validate_transform_output

    rdf_kwargs = []

    class FakeValidator:
        def __init__(self, *args, **kwargs):
            pass

        def validate(self, *args, **kwargs):
            pass

    class FakeRdfValidator(FakeValidator):
        def __init__(self, *args, **kwargs):
            rdf_kwargs.append(kwargs)

    mocker.patch('ogc.bblocks.validate.JsonValidator', FakeValidator)
    mocker.patch('ogc.bblocks.validate.RdfValidator', FakeRdfValidator)
    output_file = tmp_path / 'out.json'
    output_file.write_text('{}')

    validate_transform_output(mocker.MagicMock(), mocker.MagicMock(), 't', output_file, tmp_path / 'report.json')

    assert rdf_kwargs == [{'publish_shacl_shapes': False}]


@pytest.mark.parametrize('publish', [True, False])
def test_rdf_validator_publishes_shacl_shapes_only_when_asked(mocker, tmp_path, publish):
    import os

    from ogc.bblocks.validation.rdf import RdfValidator

    mocker.patch('ogc.bblocks.validation.rdf.Uplifter')
    published = {'ogc.example.profile': ['https://example.com/register/_sources/profile/shapes.shacl']}
    bblock = mocker.MagicMock()
    bblock.identifier = 'ogc.example.profile'
    bblock.metadata = {'shaclShapes': published}
    bblock.jsonld_context = None
    bblock.files_path = tmp_path
    (tmp_path / 'shapes.shacl').write_text('')
    register = mocker.MagicMock()
    register.get_inherited_shacl_shapes.return_value = {'ogc.example.profile': ['shapes.shacl']}

    RdfValidator(bblock, register, publish_shacl_shapes=publish)

    if publish:
        assert bblock.metadata['shaclShapes'] == {
            'ogc.example.profile': {os.path.relpath(tmp_path / 'shapes.shacl')}
        }
    else:
        assert bblock.metadata['shaclShapes'] is published
