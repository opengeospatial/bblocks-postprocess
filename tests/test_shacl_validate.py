"""Tests for ogc.bblocks.validation.rdf.shacl_validate, run against the real pyshacl
(it relies on pyshacl internals, so a mocked pyshacl would not catch upgrade breakage)."""
from rdflib import Graph, URIRef

from ogc.bblocks.validation.rdf import shacl_validate

DATA = """
@prefix ex: <http://example.com/> .
ex:a ex:dim ex:spatial .
"""

# Instance data (not an RDFS/OWL axiom), as found in SKOS vocabularies used as closures
CLOSURE = """
@prefix ex: <http://example.com/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
ex:spatial a skos:Concept .
"""

SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
ex:S a sh:NodeShape ;
  sh:targetSubjectsOf ex:dim ;
  sh:property [ sh:path ex:dim ; sh:class skos:Concept ] .
"""


def _graph(ttl):
    return Graph().parse(data=ttl, format='turtle')


def test_closure_instance_data_is_visible_to_sh_class():
    conforms, _, _, _ = shacl_validate(_graph(DATA), _graph(SHAPES), ont_graph=_graph(CLOSURE))
    assert conforms


def test_without_closure_sh_class_fails():
    conforms, _, _, _ = shacl_validate(_graph(DATA), _graph(SHAPES))
    assert not conforms


def test_data_graph_is_not_modified_by_the_closure():
    data = _graph(DATA)
    before = len(data)
    shacl_validate(data, _graph(SHAPES), ont_graph=_graph(CLOSURE))
    assert len(data) == before


def test_focus_nodes_come_from_the_data_not_the_closure():
    _, _, _, focus_nodes = shacl_validate(_graph(DATA), _graph(SHAPES), ont_graph=_graph(CLOSURE))
    assert {n for nodes in focus_nodes.values() for n in nodes} == {URIRef('http://example.com/a')}
