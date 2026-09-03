"""Tests for the structured-suffix media type fallback in
ogc.bblocks.validation.rdf._parse_closure_source (see GitHub issue #78:
a SHACL closure source served with e.g. 'text/anot+turtle' - a plain
Content-Type with an RFC 6839 structured suffix rdflib doesn't recognise -
could not be loaded at all).
"""
import pytest
from rdflib import Graph
from rdflib.plugin import PluginException

from ogc.bblocks.validation.rdf import _parse_closure_source


def test_parses_normally_when_rdflib_recognizes_the_type(mocker):
    # Fast path: no PluginException, no need to touch load_file at all.
    parse_mock = mocker.patch.object(Graph, 'parse')
    load_file_mock = mocker.patch('ogc.bblocks.validation.rdf.load_file')

    g = Graph()
    _parse_closure_source(g, 'https://example.com/ont.ttl')

    parse_mock.assert_called_once_with('https://example.com/ont.ttl')
    load_file_mock.assert_not_called()


def test_falls_back_to_structured_suffix_on_plugin_exception(mocker):
    calls = []

    def fake_parse(self, source=None, format=None, data=None, publicID=None, **kwargs):
        calls.append({'source': source, 'format': format, 'data': data, 'publicID': publicID})
        if data is None:
            raise PluginException("No plugin registered for (text/anot+turtle, Parser)")
        return self

    mocker.patch.object(Graph, 'parse', fake_parse)
    mocker.patch('ogc.bblocks.validation.rdf.load_file',
                 return_value=(b'<urn:a> <urn:b> <urn:c> .', 'text/anot+turtle'))

    g = Graph()
    _parse_closure_source(g, 'https://defs.opengis.net/prez-backend/object?_mediatype=text/turtle')

    assert len(calls) == 2
    # First attempt: plain graph.parse(source), no format hint
    assert calls[0]['format'] is None
    assert calls[0]['data'] is None
    # Second attempt: parsed from the already-fetched bytes with the suffix
    # mapped to a format, not by re-requesting with format= (which would send
    # a different Accept header - see the function's docstring).
    assert calls[1]['format'] == 'turtle'
    assert calls[1]['data'] == b'<urn:a> <urn:b> <urn:c> .'


@pytest.mark.parametrize('content_type', [
    'application/ld+json',
    'application/rdf+xml',
])
def test_maps_other_structured_suffixes(mocker, content_type):
    def fake_parse(self, source=None, format=None, data=None, **kwargs):
        if data is None:
            raise PluginException(f"No plugin registered for ({content_type}, Parser)")
        return self

    mocker.patch.object(Graph, 'parse', fake_parse)
    mocker.patch('ogc.bblocks.validation.rdf.load_file', return_value=(b'', content_type))

    g = Graph()
    # Should not raise
    _parse_closure_source(g, 'https://example.com/ont')


def test_reraises_when_suffix_is_not_recognized(mocker):
    mocker.patch.object(Graph, 'parse',
                        side_effect=PluginException("No plugin registered for (application/x-unknown, Parser)"))
    mocker.patch('ogc.bblocks.validation.rdf.load_file',
                 return_value=(b'', 'application/x-unknown'))

    g = Graph()
    with pytest.raises(PluginException):
        _parse_closure_source(g, 'https://example.com/ont')


def test_reraises_for_local_paths_without_fetching(tmp_path, mocker):
    mocker.patch.object(Graph, 'parse', side_effect=PluginException("boom"))
    load_file_mock = mocker.patch('ogc.bblocks.validation.rdf.load_file')

    g = Graph()
    with pytest.raises(PluginException):
        _parse_closure_source(g, tmp_path / 'ont.owl')

    load_file_mock.assert_not_called()
