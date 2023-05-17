from __future__ import annotations

import json
import shutil
from pathlib import Path

from ogc.na.util import validate as shacl_validate, load_yaml
from rdflib import Graph

from ogc.bblocks.util import BuildingBlock
import jsonschema

OUTPUT_SUBDIR = 'output'


def _validate_resource(filename: Path,
                       output_filename: Path,
                       resource_contents: str | None = None,
                       schema: dict | None = None,
                       jsonld_context: dict | None = None,
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
                jsonld_contents = json.dumps(jsonld_uplifted, indent=2)
                with open(output_filename.with_suffix('.jsonld'), 'w') as f:
                    f.write(jsonld_contents)
                graph = Graph().parse(data=jsonld_contents, format='json-ld')
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
            elif schema:
                try:
                    jsonschema.validate(json_doc, schema)
                except Exception as e:
                    report.append('=== JSON Schema errors ===')
                    report.append(str(e))

        if graph:
            if shacl_error:
                report.append(shacl_error)
            elif shacl_graph:
                shacl_report = shacl_validate(graph, shacl_graph)
                report.append("=== SHACL errors ===")
                report.append(shacl_report.text)

    except Exception as e:
        report.append(str(e))

    report_fn = output_filename.with_suffix('.validation.txt')
    with open(report_fn, 'w') as f:
        for line in report:
            f.write(f"{line}\n")

    return len(report) == 0


def validate_test_resources(bblock: BuildingBlock) -> bool:
    result = True

    if not bblock.tests_dir.is_dir() and not bblock.examples:
        return result

    shacl_graph = Graph()
    shacl_error = None
    try:
        for shacl_file in bblock.tests_dir.glob('*.shacl'):
            shacl_graph.parse(shacl_file)
    except Exception as e:
        shacl_error = str(e)

    json_error = None
    schema = None
    jsonld_context = None
    try:
        schema = load_yaml(content=bblock.schema_contents) if bblock.schema.is_file() else None
        jsonld_context = load_yaml(filename=bblock.jsonld_context) if bblock.jsonld_context.is_file() else None
    except Exception as e:
        json_error = str(e)

    output_dir = bblock.tests_dir.resolve() / OUTPUT_SUBDIR
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Test resources
    for fn in bblock.tests_dir.resolve().iterdir():
        output_fn = output_dir / fn.name

        result = result and _validate_resource(fn, output_fn,
                                               schema=schema,
                                               jsonld_context=jsonld_context,
                                               shacl_graph=shacl_graph,
                                               json_error=json_error,
                                               shacl_error=shacl_error)
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

                    result = result and _validate_resource(fn, output_fn,
                                                           resource_contents=code,
                                                           schema=schema,
                                                           jsonld_context=jsonld_context,
                                                           shacl_graph=shacl_graph,
                                                           json_error=json_error,
                                                           shacl_error=shacl_error)

    return result
