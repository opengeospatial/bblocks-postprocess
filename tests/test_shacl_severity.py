"""Advisory SHACL results (sh:Warning / sh:Info) vs. violations, and the shacl.allow-warnings setting."""
import json
from pathlib import Path

import pytest
from rdflib import Graph, URIRef

from ogc.bblocks.validation import (
    ValidationItemSeverity as Sev, ValidationItemSource, ValidationItemSourceType,
    ValidationReportItem, ValidationReportSection, ValidationReportEntry,
)
from ogc.bblocks.validation.rdf import RdfValidator, max_shacl_severity, shacl_validate

SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .
ex:S a sh:NodeShape ; sh:targetClass ex:Thing ;
  sh:property [ sh:path ex:%s ; sh:minCount 1 %s ] .
"""
DATA = "@prefix ex: <http://example.com/> . ex:a a ex:Thing ."


def _shapes(path='req', severity=None):
    return Graph().parse(data=SHAPES % (path, f'; sh:severity sh:{severity}' if severity else ''), format='turtle')


def _results(severity):
    return shacl_validate(Graph().parse(data=DATA, format='turtle'), _shapes(severity=severity))[1]


@pytest.mark.parametrize('sev,expected', [
    (None, Sev.ERROR), ('Violation', Sev.ERROR), ('Warning', Sev.WARNING), ('Info', Sev.INFO),
])
def test_max_severity_single(sev, expected):
    assert max_shacl_severity(_results(sev)) == expected


def test_max_severity_none_and_unknown_and_mixed():
    ok = Graph().parse(data=DATA + " ex:a ex:req 1 .", format='turtle')
    assert max_shacl_severity(shacl_validate(ok, _shapes())[1]) is None

    shapes = Graph().parse(data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://example.com/> .
        ex:S a sh:NodeShape ; sh:targetClass ex:Thing ;
          sh:property [ sh:path ex:w ; sh:minCount 1 ; sh:severity sh:Warning ] ,
                      [ sh:path ex:i ; sh:minCount 1 ; sh:severity sh:Info ] ,
                      [ sh:path ex:c ; sh:minCount 1 ; sh:severity ex:Custom ] .""", format='turtle')
    data = Graph().parse(data=DATA, format='turtle')
    # custom severity IRI is fail-safe ERROR even alongside warnings/infos
    assert max_shacl_severity(shacl_validate(data, shapes)[1]) == Sev.ERROR

    mixed = Graph()
    for s in ('Warning', 'Info'):
        mixed += _results(s)
    assert max_shacl_severity(mixed) == Sev.WARNING


def _validate(tmp_path, severity, allow, require_fail=False, extra_shapes=None):
    validator = RdfValidator.__new__(RdfValidator)
    shapes = _shapes(severity=severity)
    if extra_shapes is not None:
        shapes += extra_shapes
    validator.shacl_graphs = {Path('shapes.ttl'): shapes}
    validator.shacl_errors = []
    validator.closure_errors = []
    validator.added_shacl_closures = []
    validator.closure_graph = None
    validator.closure_graph_sources = []
    validator._ensure_shacl_closures_loaded = lambda: None
    validator._load_graph = lambda *a, **kw: Graph().parse(data=DATA, format='turtle')
    item = ValidationReportItem(ValidationItemSource(
        type=ValidationItemSourceType.TEST_RESOURCE, filename=Path('t.ttl'), require_fail=require_fail))
    ran = validator.validate(Path('t.ttl'), tmp_path / 't.ttl', item, contents='x', shacl_allow_warnings=allow)
    assert ran is True
    entries = item._sections[ValidationReportSection.SHACL]
    result_entries = [e for e in entries if (e.payload or {}).get('op') == 'shacl-result']
    (summary,) = [e for e in entries if (e.payload or {}).get('op') == 'shacl-report']
    assert summary.payload['graph'] and not summary.is_error
    return item, result_entries


@pytest.mark.parametrize('sev,expected', [('Warning', Sev.WARNING), ('Info', Sev.INFO)])
def test_advisory_fails_by_default(tmp_path, sev, expected):
    item, (entry,) = _validate(tmp_path, sev, allow=False)
    assert entry.severity == expected and entry.is_error and item.failed


@pytest.mark.parametrize('sev,expected', [('Warning', Sev.WARNING), ('Info', Sev.INFO)])
def test_advisory_passes_when_allowed(tmp_path, sev, expected):
    item, (entry,) = _validate(tmp_path, sev, allow=True)
    assert entry.severity == expected and not entry.is_error and not item.failed
    assert entry.payload['resultMessage'] and entry.message


def test_violation_fails_even_when_allowed(tmp_path):
    item, (entry,) = _validate(tmp_path, None, allow=True)
    assert entry.severity == Sev.ERROR and entry.is_error and item.failed


def test_mixed_warning_and_violation_fails_when_allowed(tmp_path):
    extra = Graph().parse(data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://example.com/> .
        ex:S2 a sh:NodeShape ; sh:targetClass ex:Thing ;
          sh:property [ sh:path ex:other ; sh:minCount 1 ] .""", format='turtle')
    item, entries = _validate(tmp_path, 'Warning', allow=True, extra_shapes=extra)
    # Each result is reported with its own severity: the violation fails the block, the warning does not
    assert {(e.severity, e.is_error) for e in entries} == {(Sev.ERROR, True), (Sev.WARNING, False)}
    assert entries[0].severity == Sev.ERROR  # most severe first
    assert item.failed


def test_fail_test_with_only_warnings_does_not_satisfy_require_fail(tmp_path):
    item, (entry,) = _validate(tmp_path, 'Warning', allow=True, require_fail=True)
    assert not entry.is_error
    assert item.failed  # a '-fail' resource must produce a real violation


def test_fail_test_with_only_warnings_still_ok_when_not_allowed(tmp_path):
    item, _ = _validate(tmp_path, 'Warning', allow=False, require_fail=True)
    assert not item.failed


def test_has_warnings_property():
    item = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.TEST_RESOURCE,
                                                     filename=Path('t.ttl')))
    assert not item.has_warnings
    item.add_entry(ValidationReportEntry(section=ValidationReportSection.SHACL, message='w',
                                         severity=Sev.WARNING))
    assert item.has_warnings and not item.failed


def test_text_report_marks_warning(tmp_path):
    item = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.TEST_RESOURCE,
                                                     filename=Path('t.ttl')))
    item.add_entry(ValidationReportEntry(section=ValidationReportSection.SHACL, message='advisory msg',
                                         severity=Sev.WARNING))
    bblock = type('B', (), {'identifier': 'x', 'name': 'X'})()
    fn = tmp_path / 'r.txt'
    item.write_text(bblock, fn)
    assert '** Validation warning **' in fn.read_text()


@pytest.mark.parametrize('config,expected', [
    (None, True),
    ({}, True),
    ({'shacl': None}, True),
    ({'shacl': {}}, True),
    ({'shacl': {'allow-warnings': True}}, True),
    ({'shacl': {'allow-warnings': False}}, False),
])
def test_allow_warnings_resolution(config, expected):
    from ogc.bblocks.util import resolve_shacl_allow_warnings
    assert resolve_shacl_allow_warnings(config) is expected


def test_json_report_severity_field(tmp_path):
    from ogc.bblocks.validate import report_to_dict
    item = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.TEST_RESOURCE,
                                                     filename=tmp_path / 't.ttl'))
    item.add_entry(ValidationReportEntry(section=ValidationReportSection.SHACL, message='w',
                                         severity=Sev.WARNING))
    item.add_entry(ValidationReportEntry(section=ValidationReportSection.SHACL, message='plain'))
    bblock = type('B', (), {'identifier': 'x', 'name': 'X'})()
    entries = report_to_dict(bblock, [item])['items'][0]['sections'][0]['entries']
    assert entries[0]['severity'] == 'warning' and entries[0]['isError'] is False
    assert 'severity' not in entries[1]


def test_html_report_marks_warnings(tmp_path):
    from ogc.bblocks.validate import report_to_dict, write_report
    def item(name, entries):
        i = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.TEST_RESOURCE,
                                                      filename=tmp_path / name))
        for e in entries:
            i.add_entry(e)
        return i
    shacl = ValidationReportSection.SHACL
    bb = type('B', (), {'identifier': 'x', 'name': 'X'})()
    warn = item('w.ttl', [ValidationReportEntry(section=shacl, message='advisory-msg', severity=Sev.WARNING)])
    ok = item('ok.ttl', [ValidationReportEntry(section=shacl, message='fine')])
    rep = report_to_dict(bb, [warn, ok])
    assert rep['hasWarnings'] and rep['items'][0]['hasWarnings'] and not rep['items'][1]['hasWarnings']
    out = tmp_path / 'r.html'
    write_report([rep], report_fn=out)
    html = out.read_text()
    assert 'Passed with warnings' in html and 'advisory-msg' in html


def _validate_empty(tmp_path, allow_empty, shacl_allow_warnings=True):
    validator = RdfValidator.__new__(RdfValidator)
    validator.shacl_graphs = {}
    validator.shacl_errors = []
    validator._load_graph = lambda *a, **kw: Graph()
    item = ValidationReportItem(ValidationItemSource(
        type=ValidationItemSourceType.EXAMPLE, filename=tmp_path / 'e.json', example_index=(1, 1)))
    validator.validate(tmp_path / 'e.json', tmp_path / 'e.json', item, contents='{"@id": "x"}',
                       allow_empty_graph=allow_empty, shacl_allow_warnings=shacl_allow_warnings)
    (entry,) = [e for e in item._sections[ValidationReportSection.FILES]
                if (e.payload or {}).get('op') == 'ttl-create']
    return item, entry


def test_empty_graph_is_an_error_by_default(tmp_path):
    item, entry = _validate_empty(tmp_path, allow_empty=False)
    assert entry.is_error and entry.severity is None and item.failed


@pytest.mark.parametrize('shacl_allow', [True, False])
def test_allowed_empty_graph_is_a_warning_independent_of_shacl_setting(tmp_path, shacl_allow):
    item, entry = _validate_empty(tmp_path, allow_empty=True, shacl_allow_warnings=shacl_allow)
    assert not entry.is_error and entry.severity == Sev.WARNING
    assert 'allow-empty-graph' in entry.message
    assert not item.failed and item.has_warnings


def test_result_payload_identifies_shape_focus_node_and_path(tmp_path):
    named = Graph().parse(data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://example.com/> .
        ex:Named a sh:NodeShape ; sh:targetClass ex:Thing ;
          sh:property [ sh:path ( ex:p [ sh:inversePath ex:q ] ) ; sh:name "Complex" ; sh:minCount 1 ] .""",
                          format='turtle')
    validator = RdfValidator.__new__(RdfValidator)
    validator.shacl_graphs = {Path('shapes.ttl'): named}
    validator.shacl_errors = []
    validator.closure_errors = []
    validator.added_shacl_closures = []
    validator.closure_graph = None
    validator.closure_graph_sources = []
    validator._ensure_shacl_closures_loaded = lambda: None
    validator._load_graph = lambda *a, **kw: Graph().parse(data=DATA, format='turtle')
    item = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.TEST_RESOURCE,
                                                     filename=Path('t.ttl')))
    validator.validate(Path('t.ttl'), tmp_path / 't.ttl', item, contents='x')
    (entry,) = [e for e in item._sections[ValidationReportSection.SHACL]
                if (e.payload or {}).get('op') == 'shacl-result']
    payload = entry.payload
    assert payload['focusNode'] == 'http://example.com/a'
    assert payload['resultPath'].endswith('p/^ex:q')
    assert payload['sourceConstraintComponent'].endswith('MinCountConstraintComponent')
    # the (blank node) property shape is identified through its named parent, name and path
    assert 'ex:Named' in payload['sourceShapeDescription'] and '"Complex"' in payload['sourceShapeDescription']
    assert 'path' in payload['sourceShapeDescription']
    assert 'Focus node: ex:a' in entry.message


def test_path_formatting():
    from ogc.bblocks.validation.rdf import format_shacl_path
    g = Graph().parse(data="""
        @prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <http://example.com/> .
        ex:s sh:path [ sh:alternativePath ( ex:a [ sh:zeroOrMorePath ex:b ] ) ] .""", format='turtle')
    path = g.value(URIRef('http://example.com/s'), URIRef('http://www.w3.org/ns/shacl#path'))
    assert format_shacl_path(path, [g], g) == 'ex:a|ex:b*'


def test_negative_tests_do_not_report_warnings(tmp_path):
    from ogc.bblocks.validate import report_to_dict
    item = ValidationReportItem(ValidationItemSource(type=ValidationItemSourceType.TEST_RESOURCE,
                                                     filename=tmp_path / 'x-fail.json', require_fail=True))
    item.add_entry(ValidationReportEntry(section=ValidationReportSection.SHACL, message='violation',
                                         is_error=True, severity=Sev.ERROR))
    item.add_entry(ValidationReportEntry(section=ValidationReportSection.SHACL, message='advisory',
                                         severity=Sev.WARNING))
    assert not item.failed and not item.has_warnings
    bb = type('B', (), {'identifier': 'x', 'name': 'X'})()
    rep = report_to_dict(bb, [item])
    assert rep['items'][0]['result'] and not rep['items'][0]['hasWarnings'] and not rep['hasWarnings']


def test_turtle_example_is_kept_as_authored(tmp_path):
    # A Turtle example must not be overwritten by an rdflib re-serialization
    ttl = ('@prefix ex: <http://example.com/> .\n'
           'ex:a ex:p ex:_b .   # authored spacing/comment must survive\n')
    out = tmp_path / 'e.ttl'
    out.write_text(ttl)
    validator = RdfValidator.__new__(RdfValidator)
    validator.shacl_graphs = {}
    validator.shacl_errors = []
    validator._load_graph = lambda *a, **kw: Graph().parse(data=ttl, format='turtle')
    item = ValidationReportItem(ValidationItemSource(
        type=ValidationItemSourceType.EXAMPLE, filename=tmp_path / 'e.ttl', example_index=(1, 1)))
    validator.validate(tmp_path / 'e.ttl', out, item, contents=ttl.encode())
    assert out.read_text() == ttl
    assert item.uplifted_files['ttl'] == (out, ttl)
