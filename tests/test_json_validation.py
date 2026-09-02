"""Tests for ogc.bblocks.validation.json_.JsonValidator's "@graph" handling (Tier 2,
docs/pytest-testing-plan.md), including the #77 fallback: a document with a top-level
"@graph" is first validated with it unwrapped (the common batch-of-instances
convention); if that fails, it's retried as the full, wrapped document (e.g. schemas
like the W3C PROV-JSONLD submission's, where "@context"/"@graph" are themselves the
required shape), and the test passes if that succeeds. JsonValidator is built by
bypassing __init__ with a canned jsonschema.Validator - no BuildingBlock/register
needed for this pure pass/fail logic.
"""
import json
from pathlib import Path

from ogc.bblocks.validation import ValidationItemSource, ValidationItemSourceType, \
    ValidationReportItem, ValidationReportSection
from ogc.bblocks.validation.json_ import JsonValidator, get_json_validator

# A plain "batch of instances" schema: "@graph" is just a wrapper array, matching the
# common convention that JsonValidator's unwrapping targets.
BATCH_SCHEMA = {
    'type': 'array',
    'items': {
        'type': 'object',
        'required': ['name'],
        'properties': {
            'name': {'type': 'string'},
        },
    },
}

# An "envelope" schema, shaped like the W3C PROV-JSONLD submission's: "@context" and
# "@graph" together are the mandatory top-level object, not a convenience wrapper.
ENVELOPE_SCHEMA = {
    'type': 'object',
    'required': ['@context', '@graph'],
    'properties': {
        '@context': {},
        '@graph': {'type': 'array'},
    },
}


def _make_validator(schema: dict) -> JsonValidator:
    validator = object.__new__(JsonValidator)
    validator.bblock = None
    validator.register = None
    validator.schema_error = None
    validator.schema_validator = get_json_validator(schema, 'file:///schema.json', None)
    return validator


def _run_validate(validator: JsonValidator, doc: dict | list, output_filename: Path) -> ValidationReportItem:
    report = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.EXAMPLE))
    validator.validate(
        filename=Path('example.json'),
        output_filename=output_filename,
        report=report,
        contents=json.dumps(doc),
    )
    return report


def _payloads(report: ValidationReportItem, section: ValidationReportSection) -> list[dict]:
    return [e.payload or {} for e in report.sections.get(section, [])]


def test_graph_less_document_validates_normally(tmp_path):
    validator = _make_validator(BATCH_SCHEMA)
    doc = [{'name': 'a'}, {'name': 'b'}]

    report = _run_validate(validator, doc, tmp_path / 'out.json')

    assert report.failed is False
    ops = [p.get('op') for p in _payloads(report, ValidationReportSection.FILES)]
    assert '@graph-unwrap' not in ops
    assert '@graph-unwrap-fallback' not in ops
    assert json.loads((tmp_path / 'out.json').read_text()) == doc


def test_graph_less_document_reports_failure(tmp_path):
    validator = _make_validator(BATCH_SCHEMA)
    doc = [{'no-name': 'a'}]

    report = _run_validate(validator, doc, tmp_path / 'out.json')

    assert report.failed is True
    schema_payloads = _payloads(report, ValidationReportSection.JSON_SCHEMA)
    assert any(p.get('result') is False for p in schema_payloads)


def test_graph_document_validates_when_unwrapped(tmp_path):
    # The common convention: "@graph" batches independently-valid instances.
    validator = _make_validator(BATCH_SCHEMA)
    doc = {'@graph': [{'name': 'a'}, {'name': 'b'}]}

    report = _run_validate(validator, doc, tmp_path / 'out.json')

    assert report.failed is False
    file_ops = [p.get('op') for p in _payloads(report, ValidationReportSection.FILES)]
    assert '@graph-unwrap' in file_ops
    assert '@graph-unwrap-fallback' not in file_ops
    # The unwrapped contents are what get written out and validated.
    assert json.loads((tmp_path / 'out.json').read_text()) == doc['@graph']


def test_graph_document_falls_back_to_full_object_when_unwrapped_fails(tmp_path):
    # The #77 case: "@graph" is a required envelope field (PROV-JSONLD-style), so the
    # unwrapped bare array fails but the full document validates.
    validator = _make_validator(ENVELOPE_SCHEMA)
    doc = {'@context': {}, '@graph': [{'@id': 'ex:1'}]}

    report = _run_validate(validator, doc, tmp_path / 'out.json')

    assert report.failed is False
    file_ops = [p.get('op') for p in _payloads(report, ValidationReportSection.FILES)]
    assert '@graph-unwrap' in file_ops
    assert '@graph-unwrap-fallback' in file_ops
    # Only one pass/fail validation outcome should be recorded for the JSON Schema
    # section - the fallback's - not a leftover error from the failed unwrapped attempt.
    schema_payloads = _payloads(report, ValidationReportSection.JSON_SCHEMA)
    assert [p.get('result') for p in schema_payloads] == [True]
    # The full, wrapped document is what gets written out - unwrapping it would have
    # discarded the required "@context".
    assert json.loads((tmp_path / 'out.json').read_text()) == doc


def test_graph_document_reports_original_error_when_both_fail(tmp_path):
    validator = _make_validator(ENVELOPE_SCHEMA)
    # Missing "@context" entirely: fails both unwrapped (bare array, wrong type) and
    # as a full object (missing required "@context").
    doc = {'@graph': [{'@id': 'ex:1'}]}

    report = _run_validate(validator, doc, tmp_path / 'out.json')

    assert report.failed is True
    file_ops = [p.get('op') for p in _payloads(report, ValidationReportSection.FILES)]
    assert '@graph-unwrap' in file_ops
    assert '@graph-unwrap-fallback' not in file_ops
    schema_payloads = _payloads(report, ValidationReportSection.JSON_SCHEMA)
    # Only the original (unwrapped) failure is reported, not a second one for the
    # full-object retry.
    assert len(schema_payloads) == 1
    assert schema_payloads[0].get('result') is False
    # The unwrapped attempt's error is about the bare array not being an object.
    assert 'object' in schema_payloads[0].get('errorMessage', '')
