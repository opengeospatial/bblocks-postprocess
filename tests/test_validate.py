"""Regression test for ogc.bblocks.validate._mime_type_for_extension.

Guards against a bug where validate_transform_output() / validate_test_resources()
passed the raw dict returned by ogc.bblocks.mimetypes.from_extension() straight
through as `file_format`, instead of unwrapping its 'mimeType' string. That dict
eventually reached NATIVE_RDF_LANGS.get(file_format, ...) in
ogc.bblocks.validation.rdf.RdfValidator._load_graph, which raised
"TypeError: unhashable type: 'dict'" for any RDF-suffixed transform output
(.ttl/.jsonld/.rdf) -- see the reported traceback in validate_transform_output.
"""
from pathlib import Path

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


def test_jsonld_context_error_item_is_a_failed_non_fatal_report_item(tmp_path):
    from ogc.bblocks.validate import jsonld_context_error_item

    try:
        try:
            raise ValueError("invalid IRI mapping")
        except ValueError as cause:
            raise Exception("Error writing context") from cause
    except Exception as e:
        item = jsonld_context_error_item(tmp_path / 'context.jsonld', e)

    assert item.failed
    assert item.source.type == ValidationItemSourceType.JSONLD_CONTEXT
    entries = item.sections[ValidationReportSection.JSON_LD]
    assert len(entries) == 1
    assert entries[0].is_error
    assert 'invalid IRI mapping' in entries[0].message


# --- Validator plugin entries (wire format -> report entries) ---

def _wire(entry):
    from ogc.bblocks.validation.plugin import _entry_from_wire
    return _entry_from_wire(entry, 'mod.Cls')


@pytest.mark.parametrize('wire, severity, is_error', [
    ({'message': 'm'}, 'info', False),
    ({'message': 'm', 'is_error': False}, 'info', False),
    ({'message': 'm', 'is_error': True}, 'error', True),
    ({'message': 'm', 'severity': 'info'}, 'info', False),
    ({'message': 'm', 'severity': 'Warning'}, 'warning', False),
    ({'message': 'm', 'severity': 'error'}, 'error', True),
    # severity wins over is_error when they disagree
    ({'message': 'm', 'severity': 'warning', 'is_error': True}, 'warning', False),
    ({'message': 'm', 'severity': 'error', 'is_error': False}, 'error', True),
])
def test_plugin_entry_severity_matrix(wire, severity, is_error):
    entry = _wire(wire)
    assert entry.severity.value == severity
    assert entry.is_error is is_error
    assert entry.section == ValidationReportSection.PLUGIN
    assert entry.payload['plugin'] == 'mod.Cls'
    assert not entry.is_global


def test_plugin_entry_unknown_severity_falls_back_to_is_error_and_is_noted(caplog):
    entry = _wire({'message': 'm', 'severity': 'eror', 'is_error': True})
    assert entry.is_error is True
    assert entry.severity.value == 'error'
    assert "'eror'" in entry.message
    assert 'unrecognized severity' in caplog.text

    entry = _wire({'message': 'm', 'severity': 'bogus'})
    assert entry.is_error is False
    assert entry.severity.value == 'info'


def test_plugin_entry_payload_is_merged_without_reserved_keys():
    entry = _wire({'message': 'm', 'payload': {'line': 3, 'subsection': 'S', 'op': 'require-fail',
                                                'isError': False, 'severity': 'x', 'message': 'y',
                                                'plugin': 'spoofed'}})
    assert entry.payload == {'line': 3, 'subsection': 'S', 'plugin': 'mod.Cls'}


def test_plugin_entry_message_is_coerced_to_str():
    assert _wire({'message': 42}).message == '42'
    assert 'without a message' in _wire({'is_error': True}).message
    assert 'without a message' in _wire({'message': None}).message


def test_plugin_entry_malformed_entry_is_reported_as_error():
    entry = _wire('not a dict')
    assert entry.is_error
    assert 'malformed entry' in entry.message
    assert "'not a dict'" in entry.message


def _plugin_report(tmp_path, monkeypatch):
    """Run PluginValidator.validate through the real harness subprocess (current interpreter)."""
    import os
    import sys
    from ogc.bblocks.validation import ValidationReportItem
    from ogc.bblocks.validation.plugin import PluginValidator

    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).parent / 'fixtures'))
    validator = PluginValidator('validator_fixture_plugin', 'FixtureValidator', [], tmp_path,
                                ['text/plain'], ['.txt'])
    venv_bin = validator._venv_dir() / 'bin'
    venv_bin.mkdir(parents=True)
    os.symlink(sys.executable, venv_bin / 'python')

    input_file = tmp_path / 'in.txt'
    input_file.write_text('hello')
    report = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.EXAMPLE))
    assert validator.validate(input_file, input_file, report, file_format='text/plain') is True
    return report


def test_plugin_validator_harness_round_trip_preserves_severity(tmp_path, monkeypatch):
    report = _plugin_report(tmp_path, monkeypatch)
    entries = report.sections[ValidationReportSection.PLUGIN]

    assert [(e.message, e.severity.value, e.is_error) for e in entries] == [
        ('legacy ok', 'info', False),
        ('legacy bad', 'error', True),
        ('just a warning', 'warning', False),
        ('info', 'info', False),
    ]
    assert entries[2].payload == {'subsection': 'Checks', 'line': 3, 'plugin': 'validator_fixture_plugin.FixtureValidator'}
    assert report.failed  # the legacy error entry


def test_plugin_warning_only_result_has_warnings_and_json_severity(tmp_path):
    from ogc.bblocks.validate import report_to_dict
    from ogc.bblocks.validation import ValidationReportItem

    report = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.EXAMPLE))
    report.add_entry(_wire({'message': 'careful', 'severity': 'warning'}))
    report.add_entry(_wire({'message': 'fine'}))

    assert not report.failed
    assert report.has_warnings

    class _BB:
        identifier = 'x.y'
        name = 'Y'

    entries = report_to_dict(_BB(), [report])['items'][0]['sections'][0]['entries']
    assert [(e['message'], e['severity'], e['isError']) for e in entries] == [
        ('careful', 'warning', False),
        ('fine', 'info', False),
    ]
    assert all(e['plugin'] == 'mod.Cls' for e in entries)


def test_plugin_harness_reports_non_list_return(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys
    from ogc.bblocks.validation.plugin import _HARNESS

    (tmp_path / 'bad_return_plugin.py').write_text(
        "class V:\n    file_extensions = ['.txt']\n    def validate(self, meta):\n        return {'message': 'x'}\n")
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    proc = subprocess.run([sys.executable, str(_HARNESS), json.dumps(
        {'module': 'bad_return_plugin', 'class_name': 'V', 'input_path': 'x'})],
        capture_output=True, text=True)
    data = json.loads(proc.stdout)
    assert data['entries'] == []
    assert 'must return a list' in data['stderr']
