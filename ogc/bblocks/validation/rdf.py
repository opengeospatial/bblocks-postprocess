import dataclasses
import json
import logging
import os
import re
from io import StringIO
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError

import pyshacl
from ogc.na.util import is_url, copy_triples
from pyparsing import ParseBaseException
from pyshacl.errors import ReportableRuntimeError
from pyshacl.graph_abstraction import DataGraph
from rdflib import Graph, Literal, Namespace, RDF
from rdflib.collection import Collection
from rdflib.plugin import PluginException
from rdflib.term import Node, URIRef, BNode

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.validation import Validator, ValidationItemSourceType, ValidationReportSection, ValidationReportEntry, \
    ValidationReportItem, ValidationItemSeverity, uplift
from ogc.bblocks.validation.uplift import Uplifter
from ogc.bblocks.util import load_yaml, load_file, PathOrUrl

NATIVE_RDF_LANGS = {
    'application/ld+json': 'jsonld',
    'text/turtle': 'ttl',
    'application/rdf+xml': 'xml',
}

STRUCTURED_SUFFIX_RE = re.compile(r'\+([a-zA-Z0-9.-]+)$')

# RFC 6839 structured syntax suffix -> rdflib format name. Not the same as
# rdflib.util.SUFFIX_FORMAT_MAP, which is keyed by file extension (e.g. 'ttl'),
# not by the suffix name a media type actually uses (e.g. 'turtle').
STRUCTURED_SUFFIX_FORMATS = {
    'turtle': 'turtle',
    'ttl': 'turtle',
    'json': 'json-ld',
    'xml': 'xml',
    'n3': 'n3',
}


def _parse_closure_source(graph: Graph, source: str | Path):
    """
    Parse an RDF closure source into `graph`, same as `graph.parse(source)`, but
    with an extra fallback for media types using an RFC 6839 structured suffix
    (e.g. `text/anot+turtle`) that rdflib does not recognise and cannot otherwise
    guess from the source's file extension - such as the OGC's own
    `defs.opengis.net`, which serves ontologies this way.

    Passing `format=` straight to `graph.parse(source)` is not equivalent: doing
    so changes the `Accept` header rdflib sends, and some servers respond
    differently (even erroring) to that header. So on this specific failure the
    content is fetched (as it would have been fetched anyway) and parsed from
    the resulting bytes, using the suffix as the format hint instead.
    """
    try:
        graph.parse(source)
        return
    except PluginException:
        if isinstance(source, Path) or not is_url(source):
            raise
        contents, content_type = load_file(source, binary=True, return_content_type=True)
        suffix_match = STRUCTURED_SUFFIX_RE.search(content_type or '')
        fmt = STRUCTURED_SUFFIX_FORMATS.get(suffix_match.group(1)) if suffix_match else None
        if not fmt:
            # No structured suffix we recognise either - nothing more we can do
            raise
        graph.parse(data=contents, format=fmt, publicID=str(source))


def shacl_validate(g: Graph, s: Graph, ont_graph: Graph | None = None) \
        -> tuple[bool, Graph, str, dict[pyshacl.Shape, Sequence[Node]]]:
    # pyshacl.Validator requires its data graph wrapped in a DataGraph
    data_graph = DataGraph.from_rdflib(g)

    validator_data_graph = data_graph
    validator_ont_graph = ont_graph
    validator_options = {'advanced': True}
    if ont_graph is not None and len(ont_graph):
        # Since pyshacl 0.22, ont_graph only contributes RDFS/OWL axioms (classes, properties and
        # relationships) to the data graph, so closure instance data (e.g. skos:Concept members
        # checked through sh:class) would be lost. Mix the whole closure into a private copy of
        # the data instead, as earlier pyshacl versions did. The copy is ours to modify, so
        # SHACL-AF rules can run in place without pyshacl cloning it again.
        mixed = Graph()
        copy_triples(g, mixed)
        copy_triples(ont_graph, mixed)
        validator_data_graph = DataGraph.from_rdflib(mixed)
        validator_ont_graph = None
        validator_options['inplace'] = True

    validator = pyshacl.Validator(validator_data_graph, shacl_graph=s, ont_graph=validator_ont_graph,
                                  options=validator_options)
    # Focus nodes are reported against the example's own data, not the mixed-in closure
    focus_nodes: dict[pyshacl.Shape, Sequence[Node]] = {shape: shape.focus_nodes(data_graph)
                                                        for shape in validator.shacl_graph.shapes
                                                        if not shape.is_property_shape}
    conforms, shacl_result, shacl_report = validator.run()
    return conforms, shacl_result, shacl_report, focus_nodes


SH = Namespace('http://www.w3.org/ns/shacl#')

_SHACL_SEVERITY = {
    SH.Violation: ValidationItemSeverity.ERROR,
    SH.Warning: ValidationItemSeverity.WARNING,
    SH.Info: ValidationItemSeverity.INFO,
}
_SEVERITY_PRIORITY = {
    ValidationItemSeverity.ERROR: 3,
    ValidationItemSeverity.WARNING: 2,
    ValidationItemSeverity.INFO: 1
}
_SEVERITY_LABEL = {
    ValidationItemSeverity.ERROR: 'Violation',
    ValidationItemSeverity.WARNING: 'Warning',
    ValidationItemSeverity.INFO: 'Info',
}

_SHACL_RESULTS_QUERY = """
PREFIX sh: <http://www.w3.org/ns/shacl#>
SELECT ?result ?severity ?focus ?path ?component ?shape ?message ?value WHERE {
  ?result a sh:ValidationResult .
  OPTIONAL { ?result sh:resultSeverity ?severity }
  OPTIONAL { ?result sh:focusNode ?focus }
  OPTIONAL { ?result sh:resultPath ?path }
  OPTIONAL { ?result sh:sourceConstraintComponent ?component }
  OPTIONAL { ?result sh:sourceShape ?shape }
  OPTIONAL { ?result sh:resultMessage ?message }
  OPTIONAL { ?result sh:value ?value }
}
"""


@dataclasses.dataclass
class ShaclResult:
    """A single sh:ValidationResult from a SHACL validation report graph."""
    severity: ValidationItemSeverity
    severity_node: Node | None = None
    focus_node: Node | None = None
    path: Node | None = None
    component: Node | None = None
    source_shape: Node | None = None
    value: Node | None = None
    messages: list[str] = dataclasses.field(default_factory=list)


def shacl_results(results_graph: Graph) -> list[ShaclResult]:
    """Read the sh:ValidationResult nodes of a SHACL report graph, most severe first.

    A missing or unknown (custom) sh:resultSeverity is treated as an error (fail-safe).
    """

    by_result: dict[Node, ShaclResult] = {}
    for row in results_graph.query(_SHACL_RESULTS_QUERY):
        result = by_result.get(row.result)
        if result is None:
            result = by_result[row.result] = ShaclResult(
                severity=_SHACL_SEVERITY.get(row.severity, ValidationItemSeverity.ERROR),
                severity_node=row.severity,
                focus_node=row.focus,
                path=row.path,
                component=row.component,
                source_shape=row.shape,
                value=row.value,
            )
        if row.message is not None and str(row.message) not in result.messages:
            result.messages.append(str(row.message))

    return sorted(by_result.values(),
                  key=lambda r: (-_SEVERITY_PRIORITY[r.severity], str(r.source_shape), str(r.focus_node),
                                 str(r.path), r.messages))


def max_shacl_severity(results_graph: Graph) -> ValidationItemSeverity | None:
    """Highest severity among the sh:ValidationResult nodes, or None if there are none."""

    results = shacl_results(results_graph)
    return results[0].severity if results else None


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


def _first_value(graphs: Sequence[Graph], s: Node, p: URIRef) -> Node | None:
    for g in graphs:
        v = g.value(s, p)
        if v is not None:
            return v
    return None


def format_shacl_path(path: Node, graphs: Sequence[Graph], fmt_graph: Graph, _depth: int = 0) -> str:
    """Render a SHACL property path (a predicate IRI or a path expression made of blank nodes)
    using SPARQL-like notation: ``a/b`` sequence, ``a|b`` alternative, ``^a`` inverse, ``a*``, ``a+``, ``a?``.

    ``graphs`` are searched, in order, for the blank node structure of complex paths.
    """

    if isinstance(path, URIRef) or _depth > 8:
        return format_node(fmt_graph, path)

    def sub(node: Node) -> str:
        text = format_shacl_path(node, graphs, fmt_graph, _depth + 1)
        # Only sequences and alternatives need grouping inside a larger expression
        composite = not isinstance(node, URIRef) and (
                any((node, RDF.first, None) in g for g in graphs)
                or _first_value(graphs, node, SH.alternativePath) is not None)
        return f"({text})" if composite else text

    for g in graphs:
        if (path, RDF.first, None) in g:
            return '/'.join(sub(item) for item in Collection(g, path))

    alternative = _first_value(graphs, path, SH.alternativePath)
    if alternative is not None:
        for g in graphs:
            if (alternative, RDF.first, None) in g:
                return '|'.join(sub(item) for item in Collection(g, alternative))
    inverse = _first_value(graphs, path, SH.inversePath)
    if inverse is not None:
        return '^' + sub(inverse)
    for pred, suffix in ((SH.zeroOrMorePath, '*'), (SH.oneOrMorePath, '+'), (SH.zeroOrOnePath, '?')):
        inner = _first_value(graphs, path, pred)
        if inner is not None:
            return sub(inner) + suffix

    return format_node(fmt_graph, path)


def describe_shacl_shape(shape: Node | None, shapes_graph: Graph, results_graph: Graph) -> str | None:
    """Human-readable identification of the shape that produced a result.

    Named shapes are shown by their IRI. For (usually blank node) property shapes, the closest named
    ancestor in the shapes graph is shown, followed by the shape's ``sh:name`` and path if any,
    e.g. ``ex:MyShape/sh:property "Label" (path ex:prop)``.
    """

    if shape is None:
        return None
    if isinstance(shape, URIRef):
        return format_node(shapes_graph, shape)

    # Blank nodes keep their ids when copied into the report graph, so look the shape up in the shapes graph
    graphs = (shapes_graph, results_graph)
    description = '/'.join(format_node(shapes_graph, n)
                           for n in (find_closest_uri(shapes_graph, shape) or ())) or format_node(shapes_graph, shape)
    name = _first_value(graphs, shape, SH.name)
    if name is not None:
        description += f' "{name}"'
    shape_path = _first_value(graphs, shape, SH.path)
    if shape_path is not None:
        description += f" (path {format_shacl_path(shape_path, graphs, shapes_graph)})"
    return description


def format_shacl_result(result: ShaclResult, data_graph: Graph, shapes_graph: Graph,
                        results_graph: Graph) -> tuple[str, dict]:
    """Build the report text and the structured payload for a single SHACL result."""

    graphs = (results_graph, shapes_graph)
    severity_label = _SEVERITY_LABEL[result.severity] if result.severity_node in _SHACL_SEVERITY \
        else format_node(shapes_graph, result.severity_node) if result.severity_node is not None else 'Violation'
    component = format_node(shapes_graph, result.component) if result.component is not None else None
    shape_description = describe_shacl_shape(result.source_shape, shapes_graph, results_graph)
    path = format_shacl_path(result.path, graphs, data_graph) if result.path is not None else None
    focus = format_node(data_graph, result.focus_node) if result.focus_node is not None else None
    if result.value is None:
        value = None
    elif isinstance(result.value, Literal):
        value = result.value.n3(data_graph.namespace_manager)
    else:
        value = format_node(data_graph, result.value)
    message = '\n'.join(result.messages) or None

    lines = [f"{severity_label}" + (f" ({component})" if component else '') + ':']
    for label, content in (('Source shape', shape_description), ('Focus node', focus), ('Result path', path),
                           ('Value', value), ('Message', message)):
        if content is not None:
            lines.append(f"  {label}: {content}")

    payload = {
        'op': 'shacl-result',
        'resultSeverity': str(result.severity_node) if result.severity_node is not None else None,
        'focusNode': str(result.focus_node) if result.focus_node is not None else None,
        'resultPath': path,
        'sourceConstraintComponent': str(result.component) if result.component is not None else None,
        'sourceShape': (str(result.source_shape) if isinstance(result.source_shape, URIRef) else None),
        'sourceShapeDescription': shape_description,
        'value': value,
        'resultMessage': message,
    }
    return '\n'.join(lines), payload


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

    def __init__(self, bblock: BuildingBlock, register: BuildingBlockRegister,
                 publish_shacl_shapes: bool = True):
        """publish_shacl_shapes: write the resolved inherited SHACL shapes (paths relative to the
        working directory, made absolute by postprocess) back into bblock.metadata for publishing.
        Disable when validating against a bblock whose metadata may already have been published,
        e.g. a transform output profile, so its published (absolute) shapes aren't overwritten."""
        super().__init__(bblock, register)

        self.jsonld_error = None
        self.jsonld_context = None
        self.jsonld_url = bblock.metadata.get('ldContext')

        try:
            ctx = bblock.jsonld_context
            if ctx is not None and ctx.is_file():
                if isinstance(ctx, PathOrUrl) and ctx.is_url:
                    self.jsonld_context = ctx.load_yaml()
                else:
                    self.jsonld_context = load_yaml(filename=ctx)
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

        self.added_shacl_closures = []
        self.closure_graph_sources: set = set()
        self.closure_errors: list[str] = []
        self._shacl_closures_loaded = False

        if publish_shacl_shapes:
            bblock.metadata['shaclShapes'] = inherited_shacl_shapes

        inherited_post_steps = register.get_inherited_post_uplift_steps(bblock.identifier)
        self.uplifter = Uplifter(self.bblock, inherited_post_steps=inherited_post_steps)

    def _ensure_shacl_closures_loaded(self):
        # Loaded lazily (only once we know there's actually an RDF graph to validate against
        # SHACL shapes) to avoid needlessly fetching closures when they won't be used.
        if self._shacl_closures_loaded:
            return
        self._shacl_closures_loaded = True
        for closure_source in self.register.get_inherited_closure_sources(self.bblock.identifier):
            try:
                # No format= hint: sources are a mix of declared SHACL closures, ontologies
                # (which may be RDF/XML, e.g. '.owl') and RDF data resources, so let rdflib
                # guess from the file extension / response Content-Type instead of assuming
                # Turtle, same as the snippet-declared closures below. _parse_closure_source
                # additionally falls back to a structured media type suffix (e.g.
                # 'text/anot+turtle') when rdflib doesn't recognise the plain Content-Type.
                _parse_closure_source(self.closure_graph, closure_source)
                self.added_shacl_closures.append(closure_source)
                self.closure_graph_sources.add(closure_source)
            except HTTPError as e:
                # A closure source is background vocabulary, not the block's own data: losing
                # one shouldn't fail every example of every block that inherits it, only make
                # SHACL validation run against a less complete closure graph.
                self.closure_errors.append(f"Error retrieving closure source {e.url}: {e}")
            except Exception as e:
                self.closure_errors.append(f"Error processing closure source {closure_source}: {str(e)}")

    def _load_graph(self, filename: Path, output_filename: Path, report: ValidationReportItem,
                    contents: str | None = None,
                    base_uri: str | None = None,
                    prefixes: dict[str, str] | None = None,
                    file_format: str | None = None,
                    resource_url: str | None = None) -> Graph | None | bool:
        graph = False
        if filename.suffix in ('.json', '.geojson') or file_format in ('application/json', 'application/geo+json'):
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

            if contents is not None:
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
                    if not isinstance(existing_context, list):
                        existing_context = [existing_context]
                    # A reference to this bblock's own published context is already inlined
                    # above; fetching it would load the previously published (or, for a local
                    # build, unreachable) version over the one being built.
                    new_context.extend(c for c in existing_context
                                       if not (self.jsonld_context and self.jsonld_url
                                               and c == self.jsonld_url))
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
            except (ValueError, SyntaxError, IOError) as e:
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

            if contents is not None:
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
            if report.source.type == ValidationItemSourceType.EXAMPLE:
                file_from = 'examples'
            elif report.source.type == ValidationItemSourceType.TRANSFORM_OUTPUT:
                file_from = 'transform outputs'
            else:
                file_from = 'test resources'
            rdf_format = NATIVE_RDF_LANGS.get(file_format,
                                              'ttl' if output_filename.suffix == '.ttl' else 'json-ld')
            try:
                if contents is not None:
                    # Prepend prefixes
                    if prefixes:
                        contents = '\n'.join(f"@prefix {k}: <{v}> ." for k, v in prefixes.items()) + '\n' + contents
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.TURTLE,
                            message=f"Prefixes are defined for {', '.join(prefixes.keys())}"
                        ))
                    # Relative IRIs in a document fetched from a URL resolve against that URL
                    graph = Graph().parse(data=contents, format=rdf_format,
                                          publicID=resource_url if resource_url and is_url(resource_url) else None)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {resource_url or filename.name} from {file_from}',
                    ))
                else:
                    graph = Graph().parse(filename, format=rdf_format)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message=f'Using {resource_url or filename.name} from {file_from}',
                    ))
            except IOError as e:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.TURTLE,
                    is_error=True,
                    message=f'Error while opening file for loading: {e}',
                    payload={
                        'exception': e.__class__.__qualname__,
                    }
                ))
                return
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
                 resource_url: str | None = None,
                 shacl_allow_warnings: bool = True,
                 allow_empty_graph: bool = False,
                 **kwargs) -> bool | None:
        graph = self._load_graph(filename, output_filename, report,
                                 contents, base_uri, prefixes, file_format=file_format,
                                 resource_url=resource_url)

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

                # An empty graph is an error unless the example declares it as expected
                # (allow-empty-graph), in which case it is a warning, independent of shacl.allow-warnings
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.FILES,
                    message=f"{'O' if graph else '**Empty** o'}utput Turtle {ttl_fn.name} created"
                            + (" (empty graph allowed by the example's allow-empty-graph)"
                               if not graph and allow_empty_graph else ''),
                    is_error=not graph and not allow_empty_graph,
                    severity=ValidationItemSeverity.WARNING if not graph and allow_empty_graph else None,
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

            self._ensure_shacl_closures_loaded()
            if self.closure_errors:
                # A closure source failing to load degrades the closure graph SHACL validates
                # against (e.g. sh:class checks against classes it would have defined), but the
                # block's own data may still be entirely valid - so this is a warning attached to
                # each affected example, not an error that fails the block outright.
                for closure_error in self.closure_errors:
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SHACL,
                        message=f"{closure_error} - "
                                "SHACL validation will run against an incomplete closure graph",
                        is_error=False,
                    ))

            if self.added_shacl_closures:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.SHACL,
                    message="Using SHACL closure graph sources "
                            "(declared closures, ontologies and RDF data resources):\n - "
                            + '\n - '.join(str(c) for c in self.added_shacl_closures),
                    is_error=False,
                ))

            if additional_shacl_closures:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.SHACL,
                    message="Using snippet SHACL closures:\n - " + '\n - '.join(additional_shacl_closures),
                    is_error=False,
                ))
                additional_shacl_closures = [c if is_url(c) else self.bblock.files_path.joinpath(c)
                                             for c in additional_shacl_closures]

            shacl_errors_found = False
            for shacl_file, shacl_graph in self.shacl_graphs.items():

                try:
                    ont_graph = Graph()
                    if additional_shacl_closures:
                        for additional_shacl_closure in additional_shacl_closures:
                            if additional_shacl_closure not in self.closure_graph_sources:
                                ont_graph.parse(additional_shacl_closure)
                    if self.closure_graph:
                        copy_triples(self.closure_graph, ont_graph)

                    shacl_conforms, shacl_result, shacl_report, focus_nodes = shacl_validate(
                        graph, shacl_graph, ont_graph=ont_graph)

                    results = shacl_results(shacl_result)
                    if not results and not shacl_conforms:
                        # Non-conforming but no readable results: fail-safe to a global error
                        shacl_errors_found = True
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.SHACL,
                            message=f"Validation result for {shacl_file}: not conforming, but no results could be read",
                            is_error=True,
                            severity=ValidationItemSeverity.ERROR,
                            payload={
                                'op': 'shacl-report',
                                'shaclFile': str(shacl_file),
                                'graph': shacl_result.serialize(),
                            }
                        ))
                    else:
                        counts = {sev: sum(1 for r in results if r.severity == sev)
                                  for sev in (ValidationItemSeverity.ERROR, ValidationItemSeverity.WARNING,
                                              ValidationItemSeverity.INFO)}
                        summary = ', '.join(f"{n} {_SEVERITY_LABEL[sev].lower()}{'' if n == 1 else 's'}"
                                            for sev, n in counts.items() if n) or 'no results'
                        # The summary entry only carries the full results graph; each result is reported
                        # in its own entry, with its own severity
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.SHACL,
                            message=f"Validation result for {shacl_file}: {summary}",
                            is_error=False,
                            payload={
                                'op': 'shacl-report',
                                'shaclFile': str(shacl_file),
                                'graph': shacl_result.serialize(),
                            }
                        ))
                        for result in results:
                            result_text, result_payload = format_shacl_result(result, graph, shacl_graph, shacl_result)
                            result_payload['shaclFile'] = str(shacl_file)
                            is_error = (result.severity == ValidationItemSeverity.ERROR) or not shacl_allow_warnings
                            if is_error:
                                shacl_errors_found = True
                            report.add_entry(ValidationReportEntry(
                                section=ValidationReportSection.SHACL,
                                message=result_text,
                                is_error=is_error,
                                severity=result.severity,
                                payload=result_payload,
                            ))
                        if results and not any(r.severity == ValidationItemSeverity.ERROR for r in results) \
                                and shacl_allow_warnings:
                            logging.warning("SHACL advisory results for %s (not failing: shacl.allow-warnings)",
                                            shacl_file)
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

            # The validator ran regardless of the SHACL outcome; that
            # outcome is conveyed via per-entry severity and is_error
            # fields. Returning False here would be misread by the caller as
            # "this validator declined to run" (the convention used elsewhere,
            # e.g. JsonValidator returning False for a non-JSON file), silently
            # dropping a resource that actually failed SHACL validation — as
            # expected for a -fail test — from the report.
            return True
        return None
