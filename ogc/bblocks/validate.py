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

OUTPUT_SUBDIR = 'output'


def _validate_resource(filename: Path,
                       output_filename: Path,
                       resource_contents: str | None = None,
                       schema_validator: jsonschema.Validator | None = None,
                       schema_url: str | None = None,
                       jsonld_context: dict | None = None,
                       jsonld_url: str | None = None,
                       shacl_graph: Graph | None = None,
                       json_error: str | None = None,
                       shacl_error: str | None = None) -> bool:
    report = []
    try:
        json_doc = None
        graph = None

        if filename.suffix in ('.json', '.jsonld'):
            if resource_contents:
                json_doc = load_yaml(content=resource_contents)
            else:
                json_doc = load_yaml(filename=filename)

            if '@graph' in json_doc:
                json_doc = json_doc['@graph']

            if filename.suffix == '.json' and jsonld_context:
                if isinstance(json_doc, dict):
                    jsonld_uplifted = {'@context': jsonld_context['@context'], **json_doc}
                else:
                    jsonld_uplifted = {
                        '@context': jsonld_context['@context'],
                        '@graph': json_doc,
                    }
                jsonld_expanded = json.dumps(pyld.jsonld.expand(jsonld_uplifted))
                graph = Graph().parse(data=jsonld_expanded, format='json-ld')

                if jsonld_url:
                    jsonld_uplifted['@context'] = jsonld_url
                with open(output_filename.with_suffix('.jsonld'), 'w') as f:
                    json.dump(jsonld_uplifted, f, indent=2)

            elif output_filename.suffix == '.jsonld':
                graph = Graph().parse(filename)

            if graph:
                graph.serialize(output_filename.with_suffix('.ttl'), format='ttl')

        elif filename.suffix == '.ttl':
            if resource_contents:
                graph = Graph().parse(data=resource_contents, format='ttl')
            else:
                graph = Graph().parse(filename)

        else:
            return True

        if json_doc:
            if json_error:
                report.append(json_error)
            elif schema_validator:
                try:
                    validate_json(json_doc, schema_validator)
                except Exception as e:
                    if not isinstance(e, jsonschema.exceptions.ValidationError):
                        import traceback
                        traceback.print_exception(e)
                    report.append('=== JSON Schema errors ===')
                    report.append(f"{type(e).__name__}: {e}")

            if schema_url:
                json_doc = {'$schema': schema_url, **json_doc}

            if resource_contents:
                # This is an example, write it to disk
                with open(output_filename, 'w') as f:
                    json.dump(json_doc, f, indent=2)

        if graph:
            if shacl_error:
                report.append(shacl_error)
            elif shacl_graph:
                shacl_report = shacl_validate(graph, shacl_graph)
                report.append("=== SHACL errors ===")
                report.append(shacl_report.text)

    except Exception as e:
        report.append(f"{type(e).__name__}: {e}")

    report_fn = output_filename.with_suffix('.validation.txt')
    with open(report_fn, 'w') as f:
        for line in report:
            f.write(f"{line}\n")

    return len(report) == 0


def validate_test_resources(bblock: BuildingBlock,
                            outputs_path: str | Path | None = None) -> tuple[bool, int]:
    result = True
    test_count = 0

    if not bblock.tests_dir.is_dir() and not bblock.examples:
        return result, test_count

    shacl_graph = Graph()
    shacl_error = None
    try:
        for shacl_file in bblock.tests_dir.glob('*.shacl'):
            shacl_graph.parse(shacl_file)
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

            result = _validate_resource(fn, output_fn,
                                        schema_validator=schema_validator,
                                        jsonld_context=jsonld_context,
                                        jsonld_url=jsonld_url,
                                        shacl_graph=shacl_graph,
                                        json_error=json_error,
                                        shacl_error=shacl_error) and result
            test_count += 1

    # Examples
    if bblock.examples:
        for example_id, example in enumerate(bblock.examples):
            for snippet_id, snippet in enumerate(example.get('snippets', ())):
                code, lang = snippet.get('code'), snippet.get('language')
                if code and lang in ('json', 'jsonld', 'ttl'):
                    fn = bblock.tests_dir / f"example_{example_id + 1}_{snippet_id + 1}.{snippet['language']}"
                    output_fn = output_dir / fn.name

                    with open(output_fn, 'w') as f:
                        f.write(code)

                    result = _validate_resource(fn, output_fn,
                                                resource_contents=code,
                                                schema_url=schema_url,
                                                schema_validator=schema_validator,
                                                jsonld_context=jsonld_context,
                                                jsonld_url=jsonld_url,
                                                shacl_graph=shacl_graph,
                                                json_error=json_error,
                                                shacl_error=shacl_error) and result
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
