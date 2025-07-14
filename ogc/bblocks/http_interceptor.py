import mimetypes
import urllib.request
from email.message import Message
from pathlib import Path
from urllib.request import Request

import requests
from requests import Response

_original_urlopen = urllib.request.urlopen
_original_requests_session_request = requests.Session.request

_url_mappings: dict[str, Path] = {}
_mocked = False


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
    print(f"Intercepted URL {url} -> {local_file}")
    with open(local_file, 'rb') as f:
        return f.read()


def enable(url_mappings: dict[str, str | Path] | None = None):
    global _url_mappings, _mocked
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