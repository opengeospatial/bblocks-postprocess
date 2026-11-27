"""
Run-wide cache for remote RDF documents (SHACL closures, ontologies, SHACL shapes).

The same documents are needed by every building block that inherits them, so each one is
downloaded once per run. Documents are kept in memory as parsed graphs while their total size
stays within MAX_CACHED_TRIPLES, and as temporary files otherwise.
"""
from __future__ import annotations

import atexit
import dataclasses
import http.client
import io
import logging
import re
import shutil
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import urlparse

import rdflib
from rdflib import Graph
from rdflib.parser import InputSource, Parser
from rdflib.plugin import PluginException, plugins

from ogc.bblocks.util import HTTP_TIMEOUT

logger = logging.getLogger(__name__)

MAX_CACHED_TRIPLES = 300_000
MAX_PARALLEL_DOWNLOADS = 8
RETRY_DELAY = 1.0

# 4xx statuses that may well succeed on a second attempt
_RETRIABLE_CLIENT_ERRORS = {408, 425, 429}

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


@dataclasses.dataclass
class _Entry:
    final_url: str = ''
    content_type: str | None = None
    path: Path | None = None
    graph: Graph | None = None
    error: Exception | None = None
    # Did not fit in the in-memory budget (which never frees up during a run)
    too_large: bool = False


class _CachedSource(InputSource):
    """
    Stands in for rdflib's URLInputSource, so that Graph.parse() picks the parser and resolves
    relative IRIs exactly as it would when fetching the URL itself.
    """

    def __init__(self, entry: _Entry):
        super().__init__(entry.final_url)
        self.url = entry.final_url
        self.content_type = entry.content_type
        self.auto_close = True
        self.setPublicId(entry.final_url)
        self.setByteStream(io.BytesIO(entry.path.read_bytes()))

    def __repr__(self):
        # Graph.parse() guesses the format from str(source) when the content type is unknown
        return self.url


_cache: dict[tuple[str, str | None], _Entry] = {}
_cached_triples = 0
_temp_dir: Path | None = None


def clear():
    global _cached_triples, _temp_dir
    _cache.clear()
    _cached_triples = 0
    if _temp_dir:
        shutil.rmtree(_temp_dir, ignore_errors=True)
        _temp_dir = None


atexit.register(clear)


def _is_remote(source) -> bool:
    return isinstance(source, str) and urlparse(source).scheme in ('http', 'https')


def _get_temp_dir() -> Path:
    global _temp_dir
    if _temp_dir is None:
        _temp_dir = Path(tempfile.mkdtemp(prefix='bblocks-rdf-'))
    return _temp_dir


def _accept_header(fmt: str | None) -> str:
    # Same values rdflib sends: some servers respond differently (or fail) depending on them
    if fmt in ('turtle', 'ttl'):
        return 'text/turtle, application/x-turtle, */*;q=0.1'
    return ', '.join(p.name for p in plugins(kind=Parser) if '/' in p.name)


def _download(url: str, fmt: str | None, temp_dir: Path) -> _Entry:
    request = urllib.request.Request(url, headers={
        'Accept': _accept_header(fmt),
        'User-Agent': f'rdflib-{rdflib.__version__} (https://rdflib.github.io/; eikeon@eikeon.com)',
    })
    # urlopen is looked up at call time so that URL mappings (http_interceptor) apply
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT[1]) as response:
        contents = response.read()
        content_type = response.headers.get('Content-Type')
        final_url = response.geturl()
    with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as f:
        f.write(contents)
    return _Entry(final_url=final_url,
                  content_type=content_type.split(';', 1)[0] if content_type else None,
                  path=Path(f.name))


def _fetch(url: str, fmt: str | None, temp_dir: Path) -> _Entry:
    """Downloads a document, retrying once. Failures are returned (and cached), not raised."""
    for attempt in (1, 2):
        try:
            return _download(url, fmt, temp_dir)
        except HTTPError as e:
            error = e
            if e.code < 500 and e.code not in _RETRIABLE_CLIENT_ERRORS:
                break
        except (OSError, http.client.HTTPException, ValueError) as e:
            error = e
        if attempt == 1:
            logger.warning("Error retrieving %s, retrying: %s", url, error)
            time.sleep(RETRY_DELAY)
    return _Entry(error=error)


def prefetch(sources: Iterable[str | Path], fmt: str | None = None):
    """Downloads, in parallel, the remote sources that are not in the cache yet."""
    pending = list(dict.fromkeys(s for s in sources if _is_remote(s) and (s, fmt) not in _cache))
    if not pending:
        return
    temp_dir = _get_temp_dir()
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_DOWNLOADS, len(pending))) as executor:
        for url, entry in zip(pending, executor.map(lambda u: _fetch(u, fmt, temp_dir), pending)):
            _cache[url, fmt] = entry


def _copy_into(source: Graph, target: Graph):
    for prefix, namespace in source.namespaces():
        target.bind(prefix, namespace, override=False)
    for triple in source:
        target.add(triple)


def _parse_entry(graph: Graph, entry: _Entry, fmt: str | None):
    try:
        graph.parse(source=_CachedSource(entry), format=fmt)
    except PluginException:
        # Media types with an RFC 6839 structured suffix that rdflib does not know (e.g.
        # 'text/anot+turtle', as served by defs.opengis.net) and no usable file extension
        suffix_match = STRUCTURED_SUFFIX_RE.search(entry.content_type or '')
        suffix_format = STRUCTURED_SUFFIX_FORMATS.get(suffix_match.group(1)) if suffix_match else None
        if fmt or not suffix_format:
            raise
        graph.parse(source=_CachedSource(entry), format=suffix_format)


def load_graph(graph: Graph, source: str | Path, fmt: str | None = None) -> Graph:
    """
    Same as `graph.parse(source, format=fmt)`, but HTTP(S) sources are downloaded only once per
    run (with a timeout). Local sources are passed straight to rdflib.
    """
    global _cached_triples

    if not _is_remote(source):
        graph.parse(source, format=fmt)
        return graph

    key = (source, fmt)
    if key not in _cache:
        _cache[key] = _fetch(source, fmt, _get_temp_dir())
    entry = _cache[key]

    if entry.error is not None:
        raise entry.error.with_traceback(None)
    if entry.graph is not None:
        _copy_into(entry.graph, graph)
        return graph
    if entry.too_large:
        _parse_entry(graph, entry, fmt)
        return graph

    parsed = Graph()
    _parse_entry(parsed, entry, fmt)
    if _cached_triples + len(parsed) <= MAX_CACHED_TRIPLES:
        # The cached graph is private: callers only ever get a copy of it
        entry.graph = parsed
        _cached_triples += len(parsed)
        entry.path.unlink(missing_ok=True)
        entry.path = None
    else:
        entry.too_large = True
    _copy_into(parsed, graph)
    return graph
