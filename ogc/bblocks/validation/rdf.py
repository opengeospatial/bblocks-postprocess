import json
import logging
import os
import re
from io import StringIO
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError

import pyshacl
from ogc.na.util import load_yaml, is_url, copy_triples
from pyparsing import ParseBaseException
from pyshacl.errors import ReportableRuntimeError
from rdflib import Graph
from rdflib.term import Node, URIRef, BNode

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.validation import Validator, ValidationItemSourceType, ValidationReportSection, ValidationReportEntry, \
    ValidationReportItem, uplift
from ogc.bblocks.validation.uplift import Uplifter


NATIVE_RDF_LANGS = {
    'application/ld+json': 'jsonld',
    'text/turtle': 'ttl',
    'application/rdf+xml': 'xml',
}


def shacl_validate(g: Graph, s: Graph, ont_graph: Graph | None = None) \
        -> tuple[bool, Graph, str, dict[pyshacl.Shape, Sequence[Node]]]:
    validator = pyshacl.Validator(g, shacl_graph=s, ont_graph=ont_graph, options={
        'advanced': True
    })
    focus_nodes: dict[pyshacl.Shape, Sequence[Node]] = {shape: shape.focus_nodes(g)
                                                        for shape in validator.shacl_graph.shapes
                                                        if not shape.is_property_shape}
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


class CaptureLogHandler(logging.StreamHandler):

    def __init__(self):
        logging.StreamHandler.__init__(self, StringIO())

    def clear(self):
        self.stream = StringIO()

    def getvalue(self):
        return self.stream.getvalue()


capture_log_handler = CaptureLogHandler()
capture_log_handler.setLevel(logging.WARN)
rdflib_logger = logging.getLogger('rdflib')


class RdfValidator(Validator):

    def __init__(self, bblock: BuildingBlock, register: BuildingBlockRegister):
        super().__init__(bblock, register)

        self.jsonld_error = None
        self.jsonld_context = None
        self.jsonld_url = bblock.metadata.get('ldContext')

        try:
            if bblock.jsonld_context.is_file():
                self.jsonld_context = load_yaml(filename=bblock.jsonld_context)
        except Exception as e:
            self.jsonld_error = f"Error loading JSON-LD context: {type(e).__name__}: {e}"

        self.closure_graph = Graph()
        self.shacl_graphs: dict[str, Graph] = {}
        self.shacl_errors: list[str] = []
        inherited_shacl_shapes = register.get_inherited_shacl_shapes(bblock.identifier)
        for shacl_bblock in list(inherited_shacl_shapes.keys()):
            bblock_shacl_files = set()
            for shacl_file in inherited_shacl_shapes[shacl_bblock]:
                if isinstance(shacl_file, Path) or (isinstance(shacl_file, str) and not is_url(shacl_file)):
                    # assume file
                    shacl_file = str(os.path.relpath(bblock.files_path / shacl_file))
                bblock_shacl_files.add(shacl_file)
                try:
                    self.shacl_graphs[shacl_file] = Graph().parse(shacl_file, format='turtle')
                except HTTPError as e:
                    self.shacl_errors.append(f"Error retrieving {e.url}: {e}")
                except Exception as e:
                    self.shacl_errors.append(f"Error processing {shacl_file}: {str(e)}")
            inherited_shacl_shapes[shacl_bblock] = bblock_shacl_files

        for shacl_closure in bblock.shaclClosures or ():
            try:
                self.closure_graph.parse(bblock.resolve_file(shacl_closure), format='turtle')
            except HTTPError as e:
                self.shacl_errors.append(f"Error retrieving {e.url}: {e}")
            except Exception as e:
                self.shacl_errors.append(f"Error processing {shacl_closure}: {str(e)}")

        bblock.metadata['shaclShapes'] = inherited_shacl_shapes

        self.uplifter = Uplifter(self.bblock)

    def _load_graph(self, filename: Path, output_filename: Path, report: ValidationReportItem,
                    contents: str | None = None,
                    base_uri: str | None = None,
                    prefixes: dict[str, str] | None = None,
                    file_format: str | None = None) -> Graph | None | bool:
        graph = False
        if filename.suffix == '.json' or file_format == 'application/json':
            if self.jsonld_error:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_LD,
                    message=self.jsonld_error,
                    is_error=True,
                    is_global=True,
                ))
                return

            if not self.jsonld_context and not prefixes:
                return

            report.add_entry(ValidationReportEntry(
                section=ValidationReportSection.FILES,
                message='JSON-LD context is present - uplifting',
                payload={
                    'op': 'jsonld-uplift'
                }
            ))

            if contents:
                json_doc = load_yaml(content=contents)
            else:
                json_doc = load_yaml(filename=filename)

            # Additional steps for semantic uplift
            json_doc = self.uplifter.pre_uplift(report, json_doc)

            new_context = [self.jsonld_context['@context'] if self.jsonld_context else {}]

            if prefixes:
                new_context.insert(0, prefixes)

            # Preprend bblock context to snippet context
            if isinstance(json_doc, dict):
                if '@context' in json_doc:
                    existing_context = json_doc['@context']
                    if isinstance(existing_context, list):
                        new_context.extend(existing_context)
                    else:
                        new_context.append(existing_context)
                jsonld_uplifted = json_doc.copy()
                jsonld_uplifted.pop('@context', None)
                jsonld_uplifted = {
                    '@context': new_context if len(new_context) > 1 else new_context[0],
                    **jsonld_uplifted,
                }
            else:
                jsonld_uplifted = {
                    '@context': new_context if len(new_context) > 1 else new_context[0],
                    '@graph': json_doc,
                }

            try:
                capture_log_handler.clear()
                rdflib_logger.addHandler(capture_log_handler)
                graph = Graph().parse(data=json.dumps(jsonld_uplifted), format='json-ld', base=base_uri)
                uplift_error = capture_log_handler.getvalue()
            except (ValueError, SyntaxError) as e:
                uplift_error = f"{e.__class__.__qualname__}: {e}"
            finally:
                rdflib_logger.removeHandler(capture_log_handler)

            if uplift_error:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_LD,
                    is_error=True,
                    message=f'Error found when uplifting JSON-LD: {uplift_error}',
                    payload={
                        'op': 'jsonld-uplift-error',
                    }
                ))
                return

            jsonld_url = self.bblock.metadata.get('ldContext')
            if jsonld_url:
                if isinstance(jsonld_uplifted['@context'], list):
                    jsonld_uplifted['@context'][1 if prefixes else 0] = jsonld_url
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

            if graph:
                graph = self.uplifter.post_uplift(report, graph)

        elif output_filename.suffix == '.jsonld' or file_format == 'application/ld+json':

            if contents:
                jsonld_doc = load_yaml(content=contents)
            else:
                jsonld_doc = load_yaml(filename=filename)

            if prefixes:
                if isinstance(jsonld_doc, dict):
                    jsonld_doc.setdefault('@context', {})
                    if isinstance(jsonld_doc['@context'], list):
                        jsonld_doc['@context'].insert(0, {'@context': prefixes})
                    else:
                        jsonld_doc['@context'] = [
                            {'@context': prefixes},
                            jsonld_doc['@context'],
                        ]

                elif isinstance(jsonld_doc, list):
                    jsonld_doc = {
                        '@context': prefixes,
                        '@graph': jsonld_doc,
                    }

            graph = Graph().parse(data=json.dumps(jsonld_doc), format='json-ld', base=base_uri)
            graph = self.uplifter.post_uplift(report, graph)

        elif (output_filename.suffix in ('.ttl', '.jsonld', '.rdf')
              or file_format in NATIVE_RDF_LANGS):
            file_from = 'examples' if report.source.type == ValidationItemSourceType.EXAMPLE else 'test resources'
            rdf_format = NATIVE_RDF_LANGS.get(file_format,
                                              'ttl' if output_filename.suffix == '.ttl' else 'json-ld')
            try:
                if contents:
                    # Prepend prefixes
                    if prefixes:
                        contents = '\n'.join(f"@prefix {k}: <{v}> ." for k, v in prefixes.items()) + '\n' + contents
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.TURTLE,
                            message=f"Prefixes are defined for {', '.join(prefixes.keys())}"
                        ))
                    graph = Graph().parse(data=contents, format=rdf_format)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {filename.name} from {file_from}',
                    ))
                else:
                    graph = Graph().parse(filename, format=rdf_format)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {filename.name} from {file_from}',
                    ))
            except (ValueError, SyntaxError) as e:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.TURTLE,
                    is_error=True,
                    message=str(e),
                    payload={
                        'exception': e.__class__.__qualname__,
                    }
                ))
                return

        return graph

    def validate(self, filename: Path, output_filename: Path, report: ValidationReportItem,
                 contents: str | None = None,
                 base_uri: str | None = None,
                 additional_shacl_closures: list[str | Path] | None = None,
                 prefixes: dict[str, str] | None = None,
                 file_format: str | None = None,
                 **kwargs) -> bool | None:
        graph = self._load_graph(filename, output_filename, report,
                                 contents, base_uri, prefixes, file_format=file_format)

        if graph is False:
            return False
        if graph is None:
            return None

        if graph is not None and (contents or filename.suffix != '.ttl'):
            try:
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
                    is_error=not graph,
                    payload={
                        'op': 'ttl-create',
                        'empty': not graph,
                        'filename': ttl_fn.name,
                        'size': len(graph),
                    }
                ))
            except Exception as e:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.TURTLE,
                    is_error=True,
                    message=str(e),
                    payload={
                        'exception': e.__class__.__qualname__,
                    }
                ))
                return None

        if graph:
            if self.shacl_errors:
                for shacl_error in self.shacl_errors:
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=shacl_error,
                        is_error=True,
                        is_global=True,
                    ))
                    return None
            if not self.shacl_graphs:
                return None

            if additional_shacl_closures:
                additional_shacl_closures = [c if is_url(c) else self.bblock.files_path.joinpath(c)
                                             for c in additional_shacl_closures]

            shacl_errors_found = False
            for shacl_file, shacl_graph in self.shacl_graphs.items():

                try:
                    ont_graph = Graph()
                    if additional_shacl_closures:
                        for additional_shacl_closure in additional_shacl_closures:
                            ont_graph.parse(additional_shacl_closure)
                    if self.closure_graph:
                        copy_triples(self.closure_graph, ont_graph)

                    shacl_conforms, shacl_result, shacl_report, focus_nodes = shacl_validate(
                        graph, shacl_graph, ont_graph=ont_graph)

                    if not shacl_conforms:
                        shacl_errors_found = True

                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=f"Validation result for {shacl_file}:\n"
                                f"{re.sub(r'^', '  ', shacl_report, flags=re.M)}",
                        is_error=not shacl_conforms,
                        payload={
                            'op': 'shacl-report',
                            'shaclFile': str(shacl_file),
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
                            message=f"Focus nodes for {shacl_file}:\n{focus_nodes_report}",
                            payload={
                                'shaclFile': str(shacl_file),
                                'focusNodes': focus_nodes_payload,
                            }
                        ))
                except ParseBaseException as e:
                    if e.args:
                        query_lines = e.args[0].splitlines()
                        max_line_digits = len(str(len(query_lines)))
                        query_error = ('\nfor SPARQL query\n'
                                       + '\n'.join(f"{str(i + 1).rjust(max_line_digits)}: {line}"
                                                   for i, line in enumerate(query_lines)))
                    else:
                        query_error = ''
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=f"Error parsing SHACL validator for {shacl_file}: {e}{query_error}",
                        is_error=True,
                        is_global=True,
                        payload={
                            'exception': e.__class__.__qualname__,
                            'errorMessage': query_error,
                            'shaclFile': str(shacl_file),
                        }
                    ))
                    shacl_errors_found = True
                except ReportableRuntimeError as e:
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=f"Error running SHACL validation for {shacl_file}: {e}",
                        is_error=True,
                        is_global=True,
                        payload={
                            'exception': e.__class__.__qualname__,
                            'errorMessage': str(e),
                            'shaclFile': str(shacl_file),
                        }
                    ))
                    shacl_errors_found = True
            return not shacl_errors_found
        return None
