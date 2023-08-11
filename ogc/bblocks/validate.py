from __future__ import annotations

import json
import os
import shutil
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from urllib.request import urlopen

import jsonschema
import requests
from jsonschema.validators import validator_for
from ogc.na.util import load_yaml, is_url
from pyparsing import ParseBaseException
from rdflib import Graph
from rdflib.term import Node, URIRef, BNode
from yaml import MarkedYAMLError

from ogc.bblocks.util import BuildingBlock
import traceback
import pyshacl
import jsonref

OUTPUT_SUBDIR = 'output'
FORMAT_ALIASES = {
    'turtle': 'ttl',
    'json-ld': 'jsonld',
}
DEFAULT_UPLIFT_FORMATS = ['jsonld', 'ttl']


class ValidationReport:

    def __init__(self, require_fail: bool = False):
        self._errors = False
        self._sections: dict[str, list[str]] = {}
        self.uplifted_files: dict[str, str] = {}
        self.require_fail = require_fail

    def add_info(self, section, text):
        self._sections.setdefault(section, []).append(text)

    def add_error(self, section, text):
        self._errors = True
        self.add_info(section, f"\n** Validation error **\n{text}")

    def write(self, report_fn: Path):
        with open(report_fn, 'w') as f:
            for section, lines in self._sections.items():
                f.write(f"=== {section} ===\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write(f"=== End {section} ===\n\n")

    @property
    def has_errors(self) -> bool:
        return self.require_fail != self._errors


def _validate_resource(filename: Path,
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
                       schema_ref: str | None = None) -> ValidationReport:

    require_fail = filename.stem.endswith('-fail')
    report = ValidationReport(require_fail)
    unknown_errors = False

    def validate_inner():
        json_doc = None
        graph = None

        if filename.suffix in ('.json', '.jsonld'):
            try:
                if resource_contents:
                    json_doc = load_yaml(content=resource_contents)
                    report.add_info('Files', f'Using {filename.name} from examples')
                else:
                    json_doc = load_yaml(filename=filename)
                    report.add_info('Files', f'Using {filename.name}')
                json_doc = jsonref.replace_refs(json_doc, base_uri=filename.as_uri(), merge_props=True, proxies=False)
            except MarkedYAMLError as e:
                report.add_error('JSON Schema', f"Error parsing JSON example: {str(e)} "
                                                f"on or near line {e.context_mark.line + 1} "
                                                f"column {e.context_mark.column + 1}")
                return

            if '@graph' in json_doc:
                json_doc = json_doc['@graph']
                report.add_info('Files', f'"@graph" found, unwrapping')

            try:
                if filename.suffix == '.json' and jsonld_context:
                    report.add_info('Files', 'JSON-LD context is present - uplifting')
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
                    graph = Graph().parse(data=json.dumps(jsonld_uplifted), format='json-ld', base=base_uri)

                    if jsonld_url:
                        if isinstance(jsonld_uplifted['@context'], list):
                            jsonld_uplifted['@context'][0] = jsonld_url
                        else:
                            jsonld_uplifted['@context'] = jsonld_url
                    jsonld_fn = output_filename.with_suffix('.jsonld')
                    jsonld_contents = json.dumps(jsonld_uplifted, indent=2)
                    with open(jsonld_fn, 'w') as f:
                        f.write(jsonld_contents)
                        report.uplifted_files['jsonld'] = jsonld_contents
                        report.add_info('Files', f'Output JSON-LD {jsonld_fn.name} created')

                elif output_filename.suffix == '.jsonld':
                    graph = Graph().parse(data=json_doc, format='json-ld', base=base_uri)

            except JSONDecodeError as e:
                report.add_error('JSON-LD', str(e))
                return

        elif filename.suffix == '.ttl':
            try:
                if resource_contents:
                    report.add_info('Files', f'Using {filename.name} from examples')
                    graph = Graph().parse(data=resource_contents, format='ttl')
                else:
                    graph = Graph().parse(filename)
                    report.add_info('Files', f'Using {filename.name}')
            except (ValueError, SyntaxError) as e:
                report.add_error('Turtle', str(e))
                return

        else:
            return

        if graph is not None and (resource_contents or filename.suffix != '.ttl'):
            ttl_fn = output_filename.with_suffix('.ttl')
            graph.serialize(ttl_fn, format='ttl')
            report.uplifted_files['ttl'] = graph.serialize(format='ttl')
            if graph:
                report.add_info('Files', f'Output Turtle {ttl_fn.name} created')
            else:
                report.add_info('Files', f'*Empty* output Turtle {ttl_fn.name} created')

        if json_doc:
            if schema_ref:
                report.add_info('JSON Schema', f'Using the following JSON Schema: {schema_ref}')
            if json_error:
                report.add_error('JSON Schema', json_error)
            elif schema_validator:
                try:
                    validate_json(json_doc, schema_validator)
                    report.add_info('JSON Schema', 'Validation passed')
                except Exception as e:
                    if not isinstance(e, jsonschema.exceptions.ValidationError):
                        traceback.print_exception(e)
                    report.add_error('JSON Schema', f"{type(e).__name__}: {e}")

            if schema_url:
                json_doc = {'$schema': schema_url, **json_doc}

            if resource_contents:
                # This is an example, write it to disk
                with open(output_filename, 'w') as f:
                    json.dump(json_doc, f, indent=2)

        if graph:
            if shacl_error:
                report.add_error('SHACL', shacl_error)
            elif shacl_graph:
                if shacl_files:
                    report.add_info(
                        'SHACL',
                        'Using SHACL files for validation:\n - ' + '\n - '.join(str(f) for f in shacl_files)
                    )
                try:
                    shacl_conforms, shacl_result, shacl_report, focus_nodes = shacl_validate(graph, shacl_graph)
                    report_add = report.add_info if shacl_conforms else report.add_error
                    report_add('SHACL', shacl_report)
                    if focus_nodes:
                        focus_nodes_report = ''
                        for shape, shape_focus_nodes in focus_nodes.items():
                            focus_nodes_report += f" - Shape {format_node(shape.node)}"
                            shape_path = shape.path()
                            if shape_path:
                                focus_nodes_report += f" (path {shape_path})"
                            focus_nodes_report += ": "
                            if not shape_focus_nodes:
                                focus_nodes_report += '*none*'
                            else:
                                focus_nodes_report += ','.join(format_node(x) for x in shape_focus_nodes)
                            focus_nodes_report += "\n"
                        report.add_info('SHACL', 'Focus nodes:\n' + focus_nodes_report)
                except ParseBaseException as e:
                    if e.args:
                        query_lines = e.args[0].splitlines()
                        max_line_digits = len(str(len(query_lines)))
                        query_error = '\nfor SPARQL query\n' + '\n'.join(f"{str(i + 1).rjust(max_line_digits)}: {line}"
                                                                         for i, line in enumerate(query_lines))
                    else:
                        query_error = ''
                    report.add_error('SHACL', f"Error parsing SHACL validator: {e}{query_error}")

    try:
        validate_inner()
    except Exception as unknown_exc:
        report.add_error('Unknown errors', ','.join(traceback.format_exception(unknown_exc)))
        unknown_errors = True

    failed = report.has_errors
    if require_fail and not unknown_errors:
        if failed:
            report.add_info("General", "Test was expected to fail but it did not.\n")
        else:
            report.add_info("General", "Test was expected to fail and it did.\n")

    status = 'failed' if failed else 'passed'
    report_fn = output_filename.with_suffix(f'.validation_{status}.txt')
    report.write(report_fn)

    return report


def validate_test_resources(bblock: BuildingBlock,
                            registered_items_path: Path,
                            outputs_path: str | Path | None = None) -> tuple[bool, int]:
    result = True
    test_count = 0

    if not bblock.tests_dir.is_dir() and not bblock.examples:
        return result, test_count

    shacl_graph = Graph()
    shacl_error = None

    shacl_files = []
    if bblock.shaclRules:
        try:
            for shacl_file in bblock.shaclRules:
                if isinstance(shacl_file, Path) or (isinstance(shacl_file, str) and not is_url(shacl_file)):
                    # assume file
                    shacl_file = bblock.files_path / shacl_file
                    shacl_files.append(os.path.relpath(shacl_file, registered_items_path))
                else:
                    shacl_files.append(shacl_file)
                shacl_graph.parse(shacl_file, format='turtle')
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
        output_dir = Path(outputs_path) / bblock.subdirs
    else:
        output_dir = bblock.tests_dir.resolve() / OUTPUT_SUBDIR
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Test resources
    if bblock.tests_dir.is_dir():
        for fn in bblock.tests_dir.resolve().iterdir():
            if fn.suffix not in ('.json', '.jsonld', '.ttl'):
                continue
            output_fn = output_dir / fn.name

            result = not _validate_resource(
                fn, output_fn,
                schema_validator=schema_validator,
                jsonld_context=jsonld_context,
                jsonld_url=jsonld_url,
                shacl_graph=shacl_graph,
                json_error=json_error,
                shacl_error=shacl_error,
                shacl_files=shacl_files).has_errors and result
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
                    if schema_ref.startswith('#/'):
                        schema_ref = f"{bblock.schema}{schema_ref}"
                        schema_uri = bblock.schema.with_name('snippet-schema.yaml').as_uri()
                    elif not is_url(schema_ref):
                        if '#' in schema_ref:
                            path, fragment = schema_ref.split('#', 1)
                            schema_ref = f"{bblock.schema.parent.joinpath(path)}#{fragment}"
                            schema_uri = f"{bblock.schema.parent.joinpath(path).as_uri()}#{fragment}"
                        else:
                            schema_uri = bblock.schema.parent.joinpath(schema_ref).as_uri()
                    else:
                        schema_uri = bblock.schema.as_uri()
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

                    example_result = _validate_resource(
                        fn, output_fn,
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
                        schema_ref=snippet.get('schema-ref'))
                    result = result and not example_result.has_errors
                    for file_format, file_contents in example_result.uplifted_files.items():
                        if file_format not in snippet_langs and file_format in add_snippets_formats:
                            add_snippets[file_format] = file_contents
                    test_count += 1

            if add_snippets:
                snippets = example.setdefault('snippets', [])
                for lang, code in add_snippets.items():
                    snippets.append({
                        'language': lang,
                        'code': code,
                    })

    return result, test_count


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


def shacl_validate(g: Graph, s: Graph) -> tuple[bool, Graph, str, dict[pyshacl.Shape, Sequence[Node]]]:
    validator = pyshacl.Validator(g, shacl_graph=s, options={
        'advanced': True
    })
    focus_nodes: dict[pyshacl.Shape, Sequence[Node]] = {shape: shape.focus_nodes(g)
                                                        for shape in validator.shacl_graph.shapes}
    conforms, shacl_result, shacl_report = validator.run()
    return conforms, shacl_result, shacl_report, focus_nodes


def format_node(n: Node):
    if isinstance(n, URIRef):
        return f"<{n}>"
    if isinstance(n, BNode):
        return f"_:{n}"
    return str(n)
