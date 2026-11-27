"""Tests for ogc.bblocks.validation.graph_cache, against a real local HTTP server so that
its behavior can be compared with rdflib fetching the same URLs itself.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest
from rdflib import Graph, URIRef
from rdflib.compare import isomorphic
from rdflib.plugin import PluginException

from ogc.bblocks.validation import graph_cache
from ogc.bblocks.validation.graph_cache import load_graph, prefetch

TURTLE = b'@prefix ex: <http://example.com/ns#> . <#a> ex:p <b> .'
JSONLD = b'{"@id": "http://example.com/a", "http://example.com/p": {"@id": "b"}}'
RDFXML = (b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:ex="http://example.com/ns#">'
          b'<rdf:Description rdf:about="#a"><ex:p rdf:resource="b"/></rdf:Description></rdf:RDF>')

# path -> (content type, body)
DOCUMENTS = {
    '/ont.ttl': ('text/turtle; charset=utf-8', TURTLE),
    '/doc': ('application/ld+json', JSONLD),
    '/ont.owl': ('application/rdf+xml', RDFXML),
    '/untyped': (None, TURTLE),
    '/suffix': ('text/anot+turtle', TURTLE),
    '/bad-type.ttl': ('application/x-unknown', TURTLE),
    '/bad-type': ('application/x-unknown', TURTLE),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get('Accept')))
        attempts = sum(1 for path, _ in self.server.requests if path == self.path)
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/nested/ont.ttl')
            self.end_headers()
            return
        if self.path == '/nested/ont.ttl':
            status, content_type, body = 200, 'text/turtle', TURTLE
        elif self.path == '/flaky':
            status, content_type, body = (503 if attempts == 1 else 200), 'text/turtle', TURTLE
        elif self.path == '/broken':
            status, content_type, body = 500, None, b''
        elif self.path in DOCUMENTS:
            status = 200
            content_type, body = DOCUMENTS[self.path]
        else:
            status, content_type, body = 404, None, b''
        self.send_response(status)
        if content_type:
            self.send_header('Content-Type', content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(mocker):
    mocker.patch.object(graph_cache, 'RETRY_DELAY', 0)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    httpd.requests = []
    httpd.url = f'http://127.0.0.1:{httpd.server_address[1]}'
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def paths(server):
    return [path for path, _ in server.requests]


@pytest.mark.parametrize('path,fmt', [
    ('/ont.ttl', None),
    ('/ont.ttl', 'turtle'),
    ('/doc', None),
    ('/ont.owl', None),
    ('/untyped', None),
    ('/redirect', None),
    ('/bad-type.ttl', None),
])
@pytest.mark.parametrize('max_cached_triples', [0, 1000])
def test_behaves_like_rdflib(server, mocker, path, fmt, max_cached_triples):
    mocker.patch.object(graph_cache, 'MAX_CACHED_TRIPLES', max_cached_triples)
    url = server.url + path
    expected = Graph().parse(url, format=fmt)
    rdflib_accept = server.requests[0][1]
    server.requests.clear()

    first = load_graph(Graph(), url, fmt=fmt)
    assert server.requests[0][1] == rdflib_accept
    requests = len(server.requests)
    second = load_graph(Graph(), url, fmt=fmt)

    assert len(server.requests) == requests
    for loaded in (first, second):
        assert isomorphic(loaded, expected)
        assert set(loaded.namespaces()) == set(expected.namespaces())


def test_relative_iris_resolve_against_redirected_url(server):
    graph = load_graph(Graph(), server.url + '/redirect')
    assert (URIRef(server.url + '/nested/ont.ttl#a'), None, URIRef(server.url + '/nested/b')) in graph


def test_structured_suffix_fallback_does_not_download_again(server):
    graph = load_graph(Graph(), server.url + '/suffix')
    assert len(graph) == 1
    assert paths(server) == ['/suffix']


def test_unrecognized_type_raises(server):
    with pytest.raises(PluginException):
        load_graph(Graph(), server.url + '/bad-type')


def test_small_graphs_are_cached_parsed_and_large_ones_on_disk(server, mocker):
    mocker.patch.object(graph_cache, 'MAX_CACHED_TRIPLES', 1)
    load_graph(Graph(), server.url + '/ont.ttl')
    load_graph(Graph(), server.url + '/doc')

    in_memory = graph_cache._cache[server.url + '/ont.ttl', None]
    on_disk = graph_cache._cache[server.url + '/doc', None]
    assert in_memory.graph is not None and in_memory.path is None
    assert on_disk.graph is None and on_disk.path.is_file()

    # Later loads parse from disk straight into the target graph
    copy_spy = mocker.spy(graph_cache, '_copy_into')
    assert len(load_graph(Graph(), server.url + '/doc')) == 1
    copy_spy.assert_not_called()

    graph_cache.clear()
    assert not on_disk.path.exists()


def test_changes_to_a_loaded_graph_do_not_reach_the_cache(server):
    first = load_graph(Graph(), server.url + '/ont.ttl')
    first.remove((None, None, None))
    assert len(load_graph(Graph(), server.url + '/ont.ttl')) == 1


def test_prefetch_downloads_each_document_once(server):
    urls = [server.url + '/ont.ttl', server.url + '/doc', server.url + '/missing']
    prefetch(urls + urls)
    prefetch(urls)
    for url in urls[:2]:
        load_graph(Graph(), url)
    with pytest.raises(HTTPError):
        load_graph(Graph(), urls[2])
    assert sorted(paths(server)) == ['/doc', '/missing', '/ont.ttl']


def test_server_error_is_retried(server):
    assert len(load_graph(Graph(), server.url + '/flaky')) == 1
    assert paths(server) == ['/flaky', '/flaky']


@pytest.mark.parametrize('path,attempts', [('/broken', 2), ('/missing', 1)])
def test_failures_are_remembered(server, path, attempts):
    for _ in range(3):
        with pytest.raises(HTTPError) as exc_info:
            load_graph(Graph(), server.url + path)
        assert exc_info.value.url == server.url + path
    assert len(server.requests) == attempts


def test_stalled_connection_times_out(mocker):
    urlopen = mocker.patch('urllib.request.urlopen', side_effect=TimeoutError('timed out'))
    mocker.patch.object(graph_cache, 'RETRY_DELAY', 0)
    with pytest.raises(TimeoutError):
        load_graph(Graph(), 'https://example.com/ont.ttl')
    assert urlopen.call_count == 2
    assert urlopen.call_args.kwargs['timeout'] == graph_cache.HTTP_TIMEOUT[1]


def test_url_mappings_apply(tmp_path):
    from ogc.bblocks import http_interceptor
    (tmp_path / 'ont.ttl').write_bytes(TURTLE)
    http_interceptor.enable({'https://example.com/mapped/': tmp_path})
    try:
        assert len(load_graph(Graph(), 'https://example.com/mapped/ont.ttl')) == 1
    finally:
        http_interceptor.enable()


def test_local_files_are_not_cached(tmp_path):
    file = tmp_path / 'ont.ttl'
    file.write_bytes(TURTLE)
    for source in (file, str(file)):
        assert len(load_graph(Graph(), source)) == 1
    assert not graph_cache._cache
