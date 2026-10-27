import json
import logging
import mimetypes
import urllib.request

logger = logging.getLogger(__name__)
from email.message import Message
from pathlib import Path
from urllib.request import Request

import requests
from requests import Response

_original_urlopen = urllib.request.urlopen
_original_requests_session_request = requests.Session.request

_url_mappings: dict[str, Path] = {}
_local_build_base_urls: dict[Path, str | None] = {}
_rebase_logged: set[Path] = set()
_mocked = False

LOCAL_BUILD_DIR = 'build-local'
# Only these are rewritten when rebasing; anything else (images, archives...) is served untouched
REBASED_SUFFIXES = {'.json', '.jsonld', '.yaml', '.yml', '.ttl', '.html', '.htm', '.xml', '.txt', '.md', '.csv'}


class MockHTTPResponse:
    def __init__(self, url, content, status=200, headers: dict[str, str] | None = None):
        self.url = url
        self._content = content.encode('utf-8') if isinstance(content, str) else content
        self._status = status
        self.headers = Message()
        mime_type = mimetypes.guess_type(url)[0]

        if mime_type is not None:
            self.headers.add_header('Content-Type', mime_type)
        if isinstance(headers, dict):
            for h, v in headers.items():
                self.headers.add_header(h, v)

    def read(self):
        return self._content

    def getcode(self):
        return self._status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class MockRequestsResponse(Response):
    def __init__(self, url, content, status_code=200):
        super().__init__()
        self.url = url
        self.status_code = status_code
        self._content = content
        mime_type = mimetypes.guess_type(url)[0]
        if mime_type is not None:
            self.headers['Content-Type'] = mime_type


def load_content(url: str):
    url_mapping, local_path = None, None
    for um, lp in _url_mappings.items():
        if url.startswith(um):
            url_mapping = um
            local_path = lp
            break
    if not url_mapping:
        return None

    rel_path = url[len(url_mapping):]
    if rel_path.startswith('/'):
        rel_path = rel_path[1:]
    local_file = local_path / rel_path
    if not local_file.exists():
        raise IOError(f'Local file {local_file} for URL {url} from mapping {url_mapping} does not exist')
    logger.debug("Intercepted URL %s -> %s", url, local_file)
    with open(local_file, 'rb') as f:
        content = f.read()
    if rel_path.startswith(LOCAL_BUILD_DIR + '/') and local_file.suffix.lower() in REBASED_SUFFIXES:
        content = rebase_local_build(content, url_mapping, local_path)
    return content


def _with_trailing_slash(url: str) -> str:
    return url if url.endswith('/') else url + '/'


def _local_build_base_url(local_path: Path) -> str | None:
    if local_path not in _local_build_base_urls:
        base_url = None
        register_file = local_path / LOCAL_BUILD_DIR / 'register.json'
        if register_file.is_file():
            try:
                with open(register_file, encoding='utf-8') as f:
                    base_url = json.load(f).get('baseURL')
            except Exception as e:
                logger.warning("Could not read baseURL from %s: %s", register_file, e)
        if base_url:
            base_url = _with_trailing_slash(base_url)
        _local_build_base_urls[local_path] = base_url
    return _local_build_base_urls[local_path]


def rebase_local_build(content: bytes, url_mapping: str, local_path: Path) -> bytes:
    """Rewrites a local build's own base URL in content served from <local_path>/build-local/ onto
    the URL it is mapped from.

    A local build (build.sh) uses the viewer base URL (e.g. http://localhost:9090/register/), which
    every local build shares and nothing serves during a build. Rebasing each upstream's files onto
    its own mapped URL lets the same url-mapping resolve the links between them. Only text content
    is rewritten (see load_content).
    """
    base_url = _local_build_base_url(local_path)
    new_base_url = _with_trailing_slash(url_mapping)
    if not base_url or base_url == new_base_url:
        return content
    if local_path not in _rebase_logged:
        _rebase_logged.add(local_path)
        logger.info("Rebasing local build in %s from %s to %s", local_path / LOCAL_BUILD_DIR, base_url, new_base_url)
    return content.replace(base_url.encode('utf-8'), new_base_url.encode('utf-8'))


def enable(url_mappings: dict[str, str | Path] | None = None):
    global _url_mappings, _mocked
    _local_build_base_urls.clear()
    _rebase_logged.clear()
    if url_mappings is None:
        _url_mappings.clear()
    else:
        _url_mappings = {k: Path(v) for k, v in url_mappings.items()}

    if not _mocked:

        def mock_urlopen(request: str | Request, *args, **kwargs):
            url = request if not isinstance(request, Request) else request.full_url
            content = load_content(url)
            if content is not None:
                return MockHTTPResponse(url=url, content=content)
            else:
                return _original_urlopen(request, *args, **kwargs)

        def mock_requests_session_requests(self, method, url, *args, **kwargs):
            content = load_content(url)
            if content is not None:
                return MockRequestsResponse(url=url, content=content)
            else:
                return _original_requests_session_request(self, method, url, *args, **kwargs)

        urllib.request.urlopen = mock_urlopen
        requests.Session.request = mock_requests_session_requests
        _mocked = True


def disable():
    urllib.request.urlopen = _original_urlopen
    requests.Session.request = _original_requests_session_request


# Enable with empty mappings to override elements from the moment we are imported
enable()