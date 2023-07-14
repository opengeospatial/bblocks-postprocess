from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

import jsonschema
import pyld.jsonld
import requests
from jsonschema.validators import validator_for
from ogc.na.util import validate as shacl_validate, load_yaml
from rdflib import Graph

from ogc.bblocks.util import BuildingBlock
import traceback

OUTPUT_SUBDIR = 'output'


class ValidationReport:

    def __init__(self):
        self._errors = False
        self._sections: dict[str, list[str]] = {}

    def add_info(self, section, text):
        self._sections.setdefault(section, []).append(text)

    def add_error(self, section, text):
        self._errors = True
        self.add_info(section, text)

    def write(self, basename: Path):
        status = 'failed' if self._errors else 'passed'
        report_fn = basename.with_suffix(f'.validation_{status}.txt')
        with open(report_fn, 'w') as f:
            for section, lines in self._sections.items():
                f.write(f"=== {section} ===\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write(f"=== End {section} ===\n\n")

    @property
    def has_errors(self) -> bool:
        return self._errors


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
                       shacl_files: list[Path] | None = None) -> ValidationReport:
    report = ValidationReport()
    try:
        json_doc = None
        graph = None

        if filename.suffix in ('.json', '.jsonld'):
            if resource_contents:
                json_doc = load_yaml(content=resource_contents)
                report.add_info('Files', f'Using {filename.name} from examples')
            else:
                json_doc = load_yaml(filename=filename)
                report.add_info('Files', f'Using {filename.name}')

            if '@graph' in json_doc:
                json_doc = json_doc['@graph']
                report.add_info('Files', f'"@graph" found, unwrapping')

            if filename.suffix == '.json' and jsonld_context:
                report.add_info('Files', 'JSON-LD context is present - uplifting')
                if isinstance(json_doc, dict):
                    jsonld_uplifted = {'@context': jsonld_context['@context'], **json_doc}
                else:
                    jsonld_uplifted = {
                        '@context': jsonld_context['@context'],
                        '@graph': json_doc,
                    }
                jsonld_expanded = json.dumps(pyld.jsonld.expand(jsonld_uplifted, {'base': base_uri}))
                graph = Graph().parse(data=jsonld_expanded, format='json-ld', base=base_uri)

                if jsonld_url:
                    jsonld_uplifted['@context'] = jsonld_url
                jsonld_fn = output_filename.with_suffix('.jsonld')
                with open(jsonld_fn, 'w') as f:
                    json.dump(jsonld_uplifted, f, indent=2)
                    report.add_info('Files', f'Output JSON-LD {jsonld_fn.name} created')

            elif output_filename.suffix == '.jsonld':
                graph = Graph().parse(filename)

            if graph:
                ttl_fn = output_filename.with_suffix('.ttl')
                graph.serialize(ttl_fn, format='ttl')
                report.add_info('Files', f'Output Turtle {ttl_fn.name} created')

        elif filename.suffix == '.ttl':
            if resource_contents:
                report.add_info('Files', f'Using {filename.name} from examples')
                graph = Graph().parse(data=resource_contents, format='ttl')
            else:
                graph = Graph().parse(filename)
                report.add_info('Files', f'Using {filename.name}')

        else:
            return report

        if json_doc:
            if json_error:
                report.add_error('JSON Schema', json_error)
            elif schema_validator:
                try:
                    validate_json(json_doc, schema_validator)
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
                report.add_info(
                    'SHACL',
                    'Using SHACL files for validation:\n - ' + '\n - '.join([f.name for f in shacl_files if f.is_file()])
                )
                shacl_report = shacl_validate(graph, shacl_graph)
                if shacl_report.result:
                    report.add_info('SHACL', shacl_report.text)
                else:
                    report.add_error('SHACL', shacl_report.text)

    except Exception as e:
        report.add_error('Unknown errors', ','.join(traceback.format_exception(e)))

    report.write(output_filename)

    return report


def validate_test_resources(bblock: BuildingBlock,
                            outputs_path: str | Path | None = None) -> tuple[bool, int]:
    result = True
    test_count = 0

    if not bblock.tests_dir.is_dir() and not bblock.examples:
        return result, test_count

    shacl_graph = Graph()
    shacl_error = None

    if bblock.shacl_rules.is_file():
        try:
            shacl_graph.parse(bblock.shacl_rules, format='turtle')
        except Exception as e:
            shacl_error = str(e)

    json_error = None
    schema_validator = None
    jsonld_context = None
    jsonld_url = bblock.metadata.get('ldContext')

    schema_url = next((u for u in bblock.metadata.get('schema', []) if u.endswith('.json')), None)

    try:
        if bblock.annotated_schema:
            schema_validator = get_json_validator(bblock)
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
            output_fn = output_dir / fn.name

            result = not _validate_resource(
                fn, output_fn,
                schema_validator=schema_validator,
                jsonld_context=jsonld_context,
                jsonld_url=jsonld_url,
                shacl_graph=shacl_graph,
                json_error=json_error,
                shacl_error=shacl_error,
                shacl_files=[bblock.shacl_rules]).has_errors and result
            test_count += 1

    # Examples
    if bblock.examples:
        for example_id, example in enumerate(bblock.examples):
            example_base_uri = example.get('base-uri')
            for snippet_id, snippet in enumerate(example.get('snippets', ())):
                code, lang = snippet.get('code'), snippet.get('language')
                if code and lang in ('json', 'jsonld', 'ttl'):
                    fn = bblock.tests_dir / f"example_{example_id + 1}_{snippet_id + 1}.{snippet['language']}"
                    output_fn = output_dir / fn.name

                    with open(output_fn, 'w') as f:
                        f.write(code)

                    result = not _validate_resource(
                        fn, output_fn,
                        resource_contents=code,
                        schema_url=schema_url,
                        schema_validator=schema_validator,
                        jsonld_context=jsonld_context,
                        jsonld_url=jsonld_url,
                        shacl_graph=shacl_graph,
                        json_error=json_error,
                        shacl_error=shacl_error,
                        base_uri=snippet.get('base-uri', example_base_uri),
                        shacl_files=[bblock.shacl_rules]).has_errors and result
                    test_count += 1

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


def get_json_validator(bblock: BuildingBlock) -> jsonschema.Validator:
    schema = load_yaml(content=bblock.annotated_schema_contents)
    resolver = RefResolver(
        base_uri=bblock.annotated_schema.resolve().as_uri(),
        referrer=schema,
    )
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, resolver=resolver)


def validate_json(instance: Any, validator: jsonschema.Validator):
    error = jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error
