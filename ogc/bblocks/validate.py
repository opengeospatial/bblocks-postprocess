import json

from ogc.na.util import validate as shacl_validate, load_yaml
from rdflib import Graph

from ogc.bblocks.util import BuildingBlock
import jsonschema


def validate_test_resources(bblock: BuildingBlock):
    if not bblock.tests_dir.is_dir():
        return

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

    for fn in bblock.tests_dir.iterdir():
        report = []
        try:
            json_doc = None
            graph = None

            if fn.suffix in ('.json', '.jsonld'):
                json_doc = load_yaml(filename=fn)
                if '@graph' in json_doc:
                    json_doc = json_doc['@graph']

                if fn.suffix == '.json' and jsonld_context:
                    graph = Graph().parse(json.dumps({
                        '@context': jsonld_context['@context'],
                        '@graph': json_doc,
                    }), format='json-ld')
                elif fn.suffix == '.jsonld':
                    graph = Graph().parse(fn)

            elif fn.suffix == '.ttl':
                graph = Graph().parse(fn)

            else:
                continue

            if json_doc:
                if json_error:
                    report.append(json_error)
                elif schema:
                    try:
                        jsonschema.validate(json_doc, schema)
                    except Exception as e:
                        report.append('=== JSON Schema ===')
                        report.append(str(e))

            if graph:
                if shacl_error:
                    report.append(shacl_error)
                elif shacl_graph:
                    shacl_report = shacl_validate(graph, shacl_graph)
                    report.append("=== SHACL ===")
                    report.append(shacl_report.text)

        except Exception as e:
            report.append(str(e))

        report_fn = fn.with_suffix('.validation.txt')
        with open(report_fn, 'w') as f:
            for line in report:
                f.write(f"{line}\n")
