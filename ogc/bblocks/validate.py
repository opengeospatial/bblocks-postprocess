from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import re
import shutil
from enum import Enum
from io import StringIO
from json import JSONDecodeError
from pathlib import Path
from time import time
from typing import Any, Sequence
from urllib.error import HTTPError
from urllib.parse import urlsplit, urljoin
from urllib.request import urlopen
from datetime import datetime, timezone

import jsonschema
from mako import exceptions as mako_exceptions, template as mako_template
import requests
from jsonschema.validators import validator_for
from ogc.na.util import load_yaml, is_url, copy_triples
from pyparsing import ParseBaseException
from rdflib import Graph
from rdflib.term import Node, URIRef, BNode
from yaml import MarkedYAMLError

from ogc.bblocks.util import BuildingBlock, BuildingBlockRegister
import traceback
import pyshacl
import jsonref

OUTPUT_SUBDIR = 'output'
FORMAT_ALIASES = {
    'turtle': 'ttl',
    'json-ld': 'jsonld',
}
DEFAULT_UPLIFT_FORMATS = ['jsonld', 'ttl']


class CaptureLogHandler(logging.StreamHandler):

    def __init__(self):
        logging.StreamHandler.__init__(self, StringIO())

    def clear(self):
        self.stream = StringIO()

    def getvalue(self):
        return self.stream.getvalue()


class ValidationItemSourceType(Enum):
    TEST_RESOURCE = 'Test resource'
    EXAMPLE = 'Example'


class ValidationReportSection(Enum):
    GENERAL = 'General'
    FILES = 'Files'
    JSON_SCHEMA = 'JSON Schema'
    JSON_LD = 'JSON-LD'
    TURTLE = 'Turtle'
    SHACL = 'SHACL'
    UNKNOWN = 'Unknown errors'


@dataclasses.dataclass
class ValidationItemSource:
    type: ValidationItemSourceType
    filename: Path | None = None
    example_index: int | None = None
    snippet_index: int | None = None
    language: str | None = None
    require_fail: bool = False


@dataclasses.dataclass
class ValidationReportEntry:
    section: ValidationReportSection
    message: str
    is_error: bool = False
    payload: dict | None = None
    is_global: bool = False


class ValidationReportItem:

    def __init__(self, source: ValidationItemSource):
        self._has_errors = False
        self.source = source
        self._sections: dict[ValidationReportSection, list[ValidationReportEntry]] = {}
        self._uplifted_files: dict[str, tuple[Path, str]] = {}
        self._has_general_errors = False
        self._used_files: list[tuple[Path | str, bool]] = []

    def add_entry(self, entry: ValidationReportEntry):
        self._sections.setdefault(entry.section, []).append(entry)
        if entry.is_error:
            self._has_errors = True
            if entry.is_global:
                self._has_general_errors = True

    def add_uplifted_file(self, file_format: str, path: Path, contents: str):
        self._uplifted_files[file_format] = (path, contents)

    def write_text(self, bblock: BuildingBlock, report_fn: Path):
        with open(report_fn, 'w') as f:
            f.write(f"Validation report for {bblock.identifier} - {bblock.name}\n")
            f.write(f"Generated {datetime.now(timezone.utc).astimezone().isoformat()}\n")
            for section in ValidationReportSection:
                entries = self._sections.get(section)
                if not entries:
                    continue
                f.write(f"=== {section.value} ===\n")
                for entry in entries:
                    if entry.is_error:
                        f.write("\n** Validation error **\n")
                    f.write(f"{entry.message}\n")
                f.write(f"=== End {section.value} ===\n\n")

    @property
    def failed(self) -> bool:
        return self._has_general_errors or self.source.require_fail != self._has_errors

    @property
    def general_errors(self) -> bool:
        return self._has_general_errors

    @property
    def sections(self) -> dict[ValidationReportSection, list[ValidationReportEntry]]:
        return self._sections

    @property
    def uplifted_files(self) -> dict[str, tuple[Path, str]]:
        return self._uplifted_files


capture_log_handler = CaptureLogHandler()
capture_log_handler.setLevel(logging.WARN)
rdflib_logger = logging.getLogger('rdflib')


def report_to_dict(bblock: BuildingBlock,
                   items: Sequence[ValidationReportItem] | None,
                   base_url: str | None = None) -> dict:
    result = {
        'title': f"Validation report for {bblock.identifier} - {bblock.name}",
        'bblockName': bblock.name,
        'bblockId': bblock.identifier,
        'generated': datetime.now(timezone.utc).astimezone().isoformat(),
        'result': True,
        'items': [],
    }

    global_errors = {}
    cwd = Path().resolve()

    failed_count = 0
    if items:
        for item in items:
            source = {
                'type': item.source.type.name,
                'requireFail': item.source.require_fail,
            }
            if item.failed:
                result['result'] = False
                failed_count += 1
            if item.source.filename:
                source['filename'] = str(os.path.relpath(item.source.filename, cwd))
                if base_url:
                    source['filename'] = urljoin(base_url, source['filename'])
            if item.source.example_index:
                source['exampleIndex'] = item.source.example_index
                if item.source.snippet_index:
                    source['snippetIndex'] = item.source.snippet_index
            if item.source.language:
                source['language'] = item.source.language

            sections = {}
            for section, entries in item.sections.items():
                sections[section.name] = []
                for entry in entries:
                    entry_dict = {}
                    if entry.payload:
                        for k, v in entry.payload.items():
                            if isinstance(v, Path):
                                v = str(os.path.relpath(v.resolve(), cwd))
                                if base_url:
                                    v = urljoin(base_url, v)
                            elif k == 'files' and isinstance(v, list):
                                fv = []
                                for f in v:
                                    if isinstance(f, Path):
                                        f = str(os.path.relpath(f.resolve(), cwd))
                                    if base_url:
                                        f = urljoin(base_url, f)
                                    fv.append(f)
                                v = fv
                            entry_dict[k] = v
                    entry_dict['isError'] = entry.is_error
                    entry_dict['message'] = entry.message
                    if not entry.is_global:
                        sections[section.name].append(entry_dict)
                    elif entry.is_error:
                        global_errors.setdefault(section.name, entry_dict)

            res_item = {
                'source': source,
                'result': not item.failed,
                'sections': sections,
            }
            result['items'].append(res_item)

    result['globalErrors'] = global_errors
    result['counts'] = {
        'total': len(result['items']),
        'passed': len(result['items']) - failed_count,
        'failed': failed_count,
    }

    return result


def report_to_html(json_reports: list[dict],
                   report_fn: Path | None = None) -> str | None:

    pass_count = sum(r['result'] for r in json_reports)
    counts = {
        'total': len(json_reports),
        'passed': pass_count,
        'failed': len(json_reports) - pass_count,
    }
    template = mako_template.Template(filename=str(Path(__file__).parent / 'validation/report.html.mako'))
    try:
        result = template.render(reports=json_reports, counts=counts)
    except:
        raise ValueError(mako_exceptions.text_error_template().render())

    if report_fn:
        with open(report_fn, 'w') as f:
            f.write(result)
    else:
        return result


def _validate_resource(bblock: BuildingBlock,
                       filename: Path,
                       output_filename: Path,
                       resource_contents: str | None = None,
                       schema_validator: jsonschema.Validator | None = None,
                       schema_url: str | None = None,
                       jsonld_context: dict | None = None,
                       jsonld_url: str | None = None,
                       shacl_graph: Graph | None = None,
                       json_error: str | None = None,
                       shacl_error: str | None = None,
                       base_uri: str | None = None,
                       shacl_files: list[Path | str] | None = None,
                       schema_ref: str | None = None,
                       shacl_closure_files: list[str | Path] | None = None,
                       shacl_closure: Graph | None = None) -> ValidationReportItem:

    require_fail = filename.stem.endswith('-fail')
    if resource_contents:
        example_idx, snippet_idx = re.match(r'example_(\d+)_(\d+).*', filename.name).groups()
        source = ValidationItemSource(
            type=ValidationItemSourceType.EXAMPLE,
            filename=filename,
            example_index=int(example_idx),
            snippet_index=int(snippet_idx),
            language=filename.suffix[1:],
        )
    else:
        source = ValidationItemSource(
            type=ValidationItemSourceType.TEST_RESOURCE,
            filename=filename,
            language=filename.suffix[1:],
            require_fail=filename.stem.endswith('-fail'),
        )
    report = ValidationReportItem(source)

    def validate_inner():
        json_doc = None
        graph = None

        if filename.suffix in ('.json', '.jsonld'):
            try:
                if resource_contents:
                    json_doc = load_yaml(content=resource_contents)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {filename.name} from examples',
                    ))
                else:
                    json_doc = load_yaml(filename=filename)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {filename.name} from test resources',
                    ))
                json_doc = jsonref.replace_refs(json_doc, base_uri=filename.as_uri(), merge_props=True, proxies=False)
            except MarkedYAMLError as e:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_SCHEMA,
                    message=f"Error parsing JSON example: {str(e)} "
                            f"on or near line {e.context_mark.line + 1} "
                            f"column {e.context_mark.column + 1}",
                    is_error=True,
                    payload={
                        'exception': e.__class__.__qualname__,
                        'line': e.context_mark.line + 1,
                        'col': e.context_mark.column + 1,
                    }
                ))
                return

            if '@graph' in json_doc:
                json_doc = json_doc['@graph']
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.FILES,
                    message='"@graph" found, unwrapping',
                    payload={
                        'op': '@graph-unwrap'
                    }
                ))

            try:
                if (filename.suffix == '.json' and jsonld_context
                        and (isinstance(json_doc, dict) or isinstance(json_doc, list))):
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message='JSON-LD context is present - uplifting',
                        payload={
                            'op': 'jsonld-uplift'
                        }
                    ))
                    new_context = jsonld_context['@context']
                    if isinstance(json_doc, dict):
                        if '@context' in json_doc:
                            existing_context = json_doc['@context']
                            if isinstance(existing_context, list):
                                new_context = [
                                    jsonld_context['@context'],
                                    *existing_context,
                                ]
                            else:
                                new_context = [
                                    jsonld_context['@context'],
                                    existing_context,
                                ]
                        jsonld_uplifted = json_doc.copy()
                        jsonld_uplifted['@context'] = new_context
                    else:
                        jsonld_uplifted = {
                            '@context': new_context,
                            '@graph': json_doc,
                        }

                    try:
                        capture_log_handler.clear()
                        rdflib_logger.addHandler(capture_log_handler)
                        graph = Graph().parse(data=json.dumps(jsonld_uplifted), format='json-ld', base=base_uri)
                    finally:
                        rdflib_logger.removeHandler(capture_log_handler)

                    if capture_log_handler.getvalue():
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.JSON_LD,
                            is_error=True,
                            message=f'Error found when uplifting JSON-LD: {capture_log_handler.getvalue()}',
                            payload={
                                'op': 'jsonld-uplift-error',
                                'contents': capture_log_handler.getvalue(),
                            }
                        ))

                    if jsonld_url:
                        if isinstance(jsonld_uplifted['@context'], list):
                            jsonld_uplifted['@context'][0] = jsonld_url
                        else:
                            jsonld_uplifted['@context'] = jsonld_url
                    jsonld_fn = output_filename.with_suffix('.jsonld')
                    jsonld_contents = json.dumps(jsonld_uplifted, indent=2)
                    with open(jsonld_fn, 'w') as f:
                        f.write(jsonld_contents)
                    report.add_uplifted_file('jsonld', jsonld_fn, jsonld_contents)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Output JSON-LD {jsonld_fn.name} created',
                        payload={
                            'op': 'jsonld-create',
                            'filename': jsonld_fn.name,
                        }
                    ))

                elif output_filename.suffix == '.jsonld':
                    graph = Graph().parse(data=json_doc, format='json-ld', base=base_uri)

            except JSONDecodeError as e:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_LD,
                    message=str(e),
                    payload={
                        'exception': e.__class__.__qualname__,
                    }
                ))
                return

        elif filename.suffix == '.ttl':
            try:
                if resource_contents:
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {filename.name} from examples',
                    ))
                    graph = Graph().parse(data=resource_contents, format='ttl')
                else:
                    graph = Graph().parse(filename)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {filename.name} from test resources',
                    ))
            except (ValueError, SyntaxError) as e:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.TURTLE,
                    message=str(e),
                    payload={
                        'exception': e.__class__.__qualname__,
                    }
                ))
                return

        else:
            return

        if graph is not None and (resource_contents or filename.suffix != '.ttl'):
            ttl_fn = output_filename.with_suffix('.ttl')
            if graph:
                graph.serialize(ttl_fn, format='ttl')
            else:
                with open(ttl_fn, 'w') as f:
                    f.write('# Empty Turtle file\n')
            report.add_uplifted_file('ttl', ttl_fn, graph.serialize(format='ttl'))

            report.add_entry(ValidationReportEntry(
                section=ValidationReportSection.FILES,
                message=f"{'O' if graph else '**Empty** o'}utput Turtle {ttl_fn.name} created",
                payload={
                    'op': 'ttl-create',
                    'empty': not graph,
                    'filename': ttl_fn.name,
                    'size': len(graph),
                }
            ))

        if json_doc:
            if schema_ref:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_SCHEMA,
                    message=f"Using the following JSON Schema: {schema_ref}",
                    payload={
                        'filename': schema_ref,
                    }
                ))
            if json_error:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_SCHEMA,
                    message=json_error,
                    is_error=True,
                    is_global=True,
                ))
            elif schema_validator:
                try:
                    validate_json(json_doc, schema_validator)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.JSON_SCHEMA,
                        message='Validation passed',
                        payload={
                            'op': 'validation',
                            'result': True,
                        }
                    ))
                except Exception as e:
                    if not isinstance(e, jsonschema.exceptions.ValidationError):
                        traceback.print_exception(e)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.JSON_SCHEMA,
                        message=f"{type(e).__name__}: {e}",
                        is_error=True,
                        payload={
                            'op': 'validation',
                            'result': False,
                            'exception': e.__class__.__qualname__,
                            'errorMessage': e.message,
                        }
                    ))

            if schema_url:
                json_doc = {'$schema': schema_url, **json_doc}

            if resource_contents:
                # This is an example, write it to disk
                with open(output_filename, 'w') as f:
                    json.dump(json_doc, f, indent=2)

        if graph:
            if shacl_error:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.SHACL,
                    message=shacl_error,
                    is_error=True,
                    is_global=True,
                ))
            elif shacl_graph:
                if shacl_files:
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message='Using SHACL files for validation:\n - ' + '\n - '.join(str(f) for f in shacl_files),
                        payload={
                            'op': 'shacl-files',
                            'files': [str(f) for f in shacl_files],
                        }
                    ))
                try:
                    ont_graph = Graph()
                    if shacl_closure_files:
                        for c in shacl_closure_files:
                            ont_graph.parse(c)
                    if shacl_closure:
                        copy_triples(shacl_closure, ont_graph)
                    shacl_conforms, shacl_result, shacl_report, focus_nodes = shacl_validate(
                        graph, shacl_graph, ont_graph=ont_graph)

                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=shacl_report,
                        is_error=not shacl_conforms,
                        payload={
                            'op': 'shacl-report',
                            'graph': shacl_result.serialize(),
                        }
                    ))
                    if focus_nodes:
                        focus_nodes_report = ''
                        focus_nodes_payload = {}
                        for shape, shape_focus_nodes in focus_nodes.items():
                            g = Graph()
                            for t in shacl_graph.triples((shape.node, None, None)):
                                g.add(t)
                            focus_nodes_str = '/'.join(format_node(shacl_graph, n)
                                                       for n in
                                                       (find_closest_uri(shacl_graph, shape.node) or (shape.node,)))
                            focus_nodes_payload[focus_nodes_str] = {
                                'nodes': [],
                            }
                            focus_nodes_report += f" - Shape {focus_nodes_str}"
                            shape_path = shape.path()
                            if shape_path:
                                shape_path = format_node(shacl_graph, shape_path)
                                focus_nodes_report += f" (path {shape_path})"
                                focus_nodes_payload[focus_nodes_str]['path'] = shape_path
                            focus_nodes_report += ": "
                            if not shape_focus_nodes:
                                focus_nodes_report += '*none*'
                            else:
                                fmt_shape_focus_nodes = ['/'.join(format_node(graph, n)
                                                                  for n in (find_closest_uri(graph, fn) or (fn,)))
                                                         for fn in shape_focus_nodes]
                                focus_nodes_report += ','.join(fmt_shape_focus_nodes)
                                focus_nodes_payload[focus_nodes_str]['nodes'] = fmt_shape_focus_nodes
                            focus_nodes_report += "\n"

                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.SHACL,
                            message=f"Focus nodes:\n{focus_nodes_report}",
                            payload={
                                'focusNodes': focus_nodes_payload,
                            }
                        ))
                except ParseBaseException as e:
                    if e.args:
                        query_lines = e.args[0].splitlines()
                        max_line_digits = len(str(len(query_lines)))
                        query_error = '\nfor SPARQL query\n' + '\n'.join(f"{str(i + 1).rjust(max_line_digits)}: {line}"
                                                                         for i, line in enumerate(query_lines))
                    else:
                        query_error = ''
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=f"Error parsing SHACL validator: {e}{query_error}",
                        is_error=True,
                        is_global=True,
                        payload={
                            'exception': e.__class__.__qualname__,
                            'errorMessage': query_error,
                        }
                    ))

    try:
        validate_inner()
    except Exception as unknown_exc:
        report.add_entry(ValidationReportEntry(
            section=ValidationReportSection.UNKNOWN,
            message=','.join(traceback.format_exception(unknown_exc)),
            is_error=True,
            is_global=True,
            payload={
                'exception': unknown_exc.__class__.__qualname__,
            }
        ))

    failed = report.failed
    if require_fail and not report.general_errors:
        msg = 'but it did not.' if failed else 'and it did.'
        report.add_entry(ValidationReportEntry(
            section=ValidationReportSection.GENERAL,
            message=f"Test was expected to fail {msg}",
            is_error=failed,
            payload={
                'op': 'require-fail',
            }
        ))

    status = 'failed' if failed else 'passed'
    report_fn = output_filename.with_suffix(f'.validation_{status}.txt')
    report.write_text(bblock, report_fn)

    return report


def validate_test_resources(bblock: BuildingBlock,
                            bblocks_register: BuildingBlockRegister,
                            outputs_path: Path | None = None,
                            base_url: str | None = None) -> tuple[bool, int, dict]:
    final_result = True
    test_count = 0

    if not bblock.tests_dir.is_dir() and not bblock.examples:
        return final_result, test_count, report_to_dict(bblock, None, base_url)

    shacl_graph = Graph()
    bblock_shacl_closure = Graph()
    shacl_error = None

    shacl_files = []
    try:
        for shacl_file in bblocks_register.get_inherited_shacl_rules(bblock.identifier):
            if isinstance(shacl_file, Path) or (isinstance(shacl_file, str) and not is_url(shacl_file)):
                # assume file
                shacl_file = bblock.files_path / shacl_file
                shacl_files.append(os.path.relpath(shacl_file))
            else:
                shacl_files.append(shacl_file)
            shacl_graph.parse(shacl_file, format='turtle')
        bblock.metadata['shaclRules'] = shacl_files

        for sc in bblock.shaclClosures or ():
            bblock_shacl_closure.parse(bblock.resolve_file(sc), format='turtle')
    except HTTPError as e:
        shacl_error = f"Error retrieving {e.url}: {e}"
    except Exception as e:
        shacl_error = str(e)

    json_error = None
    schema_validator = None
    jsonld_context = None
    jsonld_url = bblock.metadata.get('ldContext')

    schema_url = next((u for u in bblock.metadata.get('schema', []) if u.endswith('.json')), None)

    try:
        if bblock.annotated_schema:
            schema_validator = get_json_validator(bblock.annotated_schema_contents,
                                                  bblock.annotated_schema.resolve().as_uri())
        if bblock.jsonld_context.is_file():
            jsonld_context = load_yaml(filename=bblock.jsonld_context)
    except Exception as e:
        json_error = f"{type(e).__name__}: {e}"

    if outputs_path:
        output_dir = outputs_path / bblock.subdirs
    else:
        output_dir = bblock.tests_dir.resolve() / OUTPUT_SUBDIR
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[ValidationReportItem] = []
    # Test resources
    if bblock.tests_dir.is_dir():
        for fn in bblock.tests_dir.resolve().iterdir():
            if fn.suffix not in ('.json', '.jsonld', '.ttl'):
                continue
            output_fn = output_dir / fn.name

            test_result = _validate_resource(
                bblock, fn, output_fn,
                schema_validator=schema_validator,
                jsonld_context=jsonld_context,
                jsonld_url=jsonld_url,
                shacl_graph=shacl_graph,
                json_error=json_error,
                shacl_error=shacl_error,
                shacl_files=shacl_files,
                shacl_closure=bblock_shacl_closure)
            all_results.append(test_result)
            final_result = not test_result.failed and final_result
            test_count += 1

    # Examples
    if bblock.examples:
        for example_id, example in enumerate(bblock.examples):
            example_base_uri = example.get('base-uri')
            snippets = example.get('snippets', ())
            snippet_langs = set(snippet.get('language') for snippet in snippets)
            add_snippets = {}
            for snippet_id, snippet in enumerate(snippets):
                code, lang = snippet.get('code'), snippet.get('language')
                add_snippets_formats = snippet.get('doc-uplift-formats', DEFAULT_UPLIFT_FORMATS)

                if 'schema-ref' not in snippet:
                    snippet_schema_validator = schema_validator
                else:
                    schema_ref = snippet['schema-ref']
                    random_fn = f"example.{time()}.{random.randint(0,1000)}.yaml"
                    schema_uri = bblock.schema.with_name(random_fn).as_uri()
                    if schema_ref.startswith('#/'):
                        schema_ref = f"{bblock.schema}{schema_ref}"
                    elif not is_url(schema_ref):
                        if '#' in schema_ref:
                            path, fragment = schema_ref.split('#', 1)
                            schema_ref = f"{bblock.schema.parent.joinpath(path)}#{fragment}"
                            schema_uri = (f"{bblock.schema.parent.joinpath(path).with_name(random_fn).as_uri()}"
                                          f"#{fragment}")
                        else:
                            schema_uri = bblock.schema.parent.joinpath(schema_ref).with_name(random_fn).as_uri()
                    snippet_schema = {'$ref': schema_ref}
                    snippet_schema_validator = get_json_validator(snippet_schema,
                                                                  schema_uri)

                if isinstance(add_snippets_formats, str):
                    add_snippets_formats = [add_snippets_formats]
                elif not add_snippets_formats:
                    add_snippets_formats = []
                if code and lang in ('json', 'jsonld', 'ttl', 'json-ld', 'turtle'):
                    fn = bblock.files_path / (f"example_{example_id + 1}_{snippet_id + 1}"
                                              f".{FORMAT_ALIASES.get(snippet['language'], snippet['language'])}")
                    output_fn = output_dir / fn.name

                    with open(output_fn, 'w') as f:
                        f.write(code)

                    snippet['path'] = output_fn

                    snippet_shacl_closure: list[str | Path] = snippet.get('shacl-closure')
                    if snippet_shacl_closure:
                        snippet_shacl_closure = [c if is_url(c) else bblock.files_path.joinpath(c)
                                         for c in snippet_shacl_closure]

                    example_result = _validate_resource(
                        bblock, fn, output_fn,
                        resource_contents=code,
                        schema_url=schema_url,
                        schema_validator=snippet_schema_validator,
                        jsonld_context=jsonld_context,
                        jsonld_url=jsonld_url,
                        shacl_graph=shacl_graph,
                        json_error=json_error,
                        shacl_error=shacl_error,
                        base_uri=snippet.get('base-uri', example_base_uri),
                        shacl_files=shacl_files,
                        schema_ref=snippet.get('schema-ref'),
                        shacl_closure_files=snippet_shacl_closure,
                        shacl_closure=bblock_shacl_closure)
                    all_results.append(example_result)
                    final_result = final_result and not example_result.failed
                    for file_format, file_data in example_result.uplifted_files.items():
                        if file_format not in snippet_langs and file_format in add_snippets_formats:
                            add_snippets[file_format] = file_data
                    test_count += 1

            if add_snippets:
                snippets = example.setdefault('snippets', [])
                for lang, file_data in add_snippets.items():
                    fn, code = file_data
                    snippets.append({
                        'language': lang,
                        'code': code,
                        'path': fn,
                    })

    json_report = report_to_dict(bblock=bblock, items=all_results, base_url=base_url)
    with open(output_dir / '_report.json', 'w') as f:
        json.dump(json_report, f, indent=2)

    return final_result, test_count, json_report


class RefResolver(jsonschema.validators.RefResolver):

    def resolve_remote(self, uri):
        scheme = urlsplit(uri).scheme

        if scheme in self.handlers:
            result = self.handlers[scheme](uri)
        elif scheme in ["http", "https"]:
            result = load_yaml(content=requests.get(uri).content)
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with urlopen(uri) as url:
                result = load_yaml(content=url.read().decode("utf-8"))

        if self.cache_remote:
            self.store[uri] = result
        return result


def get_json_validator(contents, base_uri) -> jsonschema.Validator:
    if isinstance(contents, dict):
        schema = contents
    else:
        schema = load_yaml(content=contents)
    resolver = RefResolver(
        base_uri=base_uri,
        referrer=schema,
    )
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, resolver=resolver)


def validate_json(instance: Any, validator: jsonschema.Validator):
    error = jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def shacl_validate(g: Graph, s: Graph, ont_graph: Graph | None = None) \
        -> tuple[bool, Graph, str, dict[pyshacl.Shape, Sequence[Node]]]:
    validator = pyshacl.Validator(g, shacl_graph=s, ont_graph=ont_graph, options={
        'advanced': True
    })
    focus_nodes: dict[pyshacl.Shape, Sequence[Node]] = {shape: shape.focus_nodes(g)
                                                        for shape in validator.shacl_graph.shapes}
    conforms, shacl_result, shacl_report = validator.run()
    return conforms, shacl_result, shacl_report, focus_nodes


def format_node(g: Graph, n: Node):
    if isinstance(n, URIRef):
        try:
            prefix, ns, qname = g.namespace_manager.compute_qname(str(n), False)
            return f"{prefix}:{qname}"
        except:
            return f"<{n}>"
    if isinstance(n, BNode):
        return f"_:{n}"
    return str(n)


def find_closest_uri(g: Graph, n: Node, max_depth=3) -> list[URIRef] | None:
    if isinstance(n, URIRef):
        return [n]

    alt_paths = []
    for s, p in g.subject_predicates(n, True):
        if isinstance(s, URIRef):
            return [s, p]
        alt_paths.append((s, p))

    if max_depth == 1:
        return None

    for s, p in alt_paths:
        found = find_closest_uri(g, s, max_depth - 1)
        if found:
            return found + [p]

    return None
