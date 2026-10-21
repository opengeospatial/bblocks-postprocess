"""Relative IRIs in a Turtle example fetched from a URL (snippet `ref`) resolve against that URL,
as they would for any RDF client loading the document, not against the working directory."""
from pathlib import Path
from unittest.mock import MagicMock

from rdflib import URIRef

from ogc.bblocks.validation.rdf import RdfValidator

TTL = '<Observation/1> a <http://www.w3.org/ns/sosa/Observation> .'
SOSA_OBSERVATION = URIRef('http://www.w3.org/ns/sosa/Observation')


def _load(**kwargs):
    validator = RdfValidator.__new__(RdfValidator)
    return validator._load_graph(Path('example.ttl'), Path('example.ttl'), MagicMock(), contents=TTL, **kwargs)


def test_relative_iris_resolve_against_resource_url():
    graph = _load(resource_url='https://example.com/spec/examples/ex6.ttl')
    assert set(graph.subjects(None, SOSA_OBSERVATION)) == {
        URIRef('https://example.com/spec/examples/Observation/1')}


def test_local_resource_is_not_a_base():
    graph = _load(resource_url='examples/ex6.ttl')
    (subject,) = graph.subjects(None, SOSA_OBSERVATION)
    assert not str(subject).startswith('examples/')
