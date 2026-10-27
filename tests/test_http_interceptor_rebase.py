"""Tests for rebasing local builds served through url-mappings (ogc.bblocks.http_interceptor).

A local build uses the viewer base URL (http://localhost:9090/register/), shared by every local
build. When a consuming register maps an upstream's URL to its checkout, files from the upstream's
build-local/ must have that base rewritten onto the mapped URL, so their links resolve through
the same mapping rather than to a server that isn't running.
"""
import json

import pytest

from ogc.bblocks import http_interceptor

LOCAL = 'http://localhost:9090/register/'
UPSTREAM = 'https://example.github.io/upstream/'
OTHER = 'https://example.github.io/other/'


def _checkout(path, base_url):
    build = path / 'build-local'
    (build / 'annotated').mkdir(parents=True)
    (build / 'register.json').write_text(json.dumps({
        'baseURL': base_url,
        'bblocks': [{'itemIdentifier': 'ogc.example.block',
                     'schema': {'application/json': f'{base_url}build-local/annotated/schema.json'}}],
    }))
    (build / 'annotated' / 'schema.json').write_text(json.dumps({
        '$ref': f'{base_url}build-local/annotated/other.json',
    }))
    (path / '_sources').mkdir()
    (path / '_sources' / 'schema.yaml').write_text(f'description: see {LOCAL}\n')
    return path


@pytest.fixture
def mappings():
    yield http_interceptor.enable
    http_interceptor.enable(None)


def test_local_build_rebased_onto_mapped_url(tmp_path, mappings):
    mappings({UPSTREAM: _checkout(tmp_path / 'upstream', LOCAL)})

    register = json.loads(http_interceptor.load_content(f'{UPSTREAM}build-local/register.json'))
    schema = json.loads(http_interceptor.load_content(f'{UPSTREAM}build-local/annotated/schema.json'))

    assert register['baseURL'] == UPSTREAM
    assert register['bblocks'][0]['schema']['application/json'] == f'{UPSTREAM}build-local/annotated/schema.json'
    assert schema['$ref'] == f'{UPSTREAM}build-local/annotated/other.json'


def test_each_upstream_rebased_onto_its_own_url(tmp_path, mappings):
    mappings({
        UPSTREAM: _checkout(tmp_path / 'upstream', LOCAL),
        OTHER.rstrip('/'): _checkout(tmp_path / 'other', LOCAL),
    })

    upstream = json.loads(http_interceptor.load_content(f'{UPSTREAM}build-local/annotated/schema.json'))
    other = json.loads(http_interceptor.load_content(f'{OTHER}build-local/annotated/schema.json'))

    assert upstream['$ref'] == f'{UPSTREAM}build-local/annotated/other.json'
    assert other['$ref'] == f'{OTHER}build-local/annotated/other.json'


def test_files_outside_local_build_untouched(tmp_path, mappings):
    mappings({UPSTREAM: _checkout(tmp_path / 'upstream', LOCAL)})

    content = http_interceptor.load_content(f'{UPSTREAM}_sources/schema.yaml')

    assert content.decode() == f'description: see {LOCAL}\n'


def test_build_with_mapped_base_untouched(tmp_path, mappings):
    mappings({UPSTREAM: _checkout(tmp_path / 'upstream', UPSTREAM)})

    schema = json.loads(http_interceptor.load_content(f'{UPSTREAM}build-local/annotated/schema.json'))

    assert schema['$ref'] == f'{UPSTREAM}build-local/annotated/other.json'


def test_mapping_to_export_dir_untouched(tmp_path, mappings):
    # e.g. 'https://.../build/' -> a directory holding a build, not a checkout with build-local/
    export = tmp_path / 'export'
    export.mkdir()
    (export / 'register.json').write_text(json.dumps({'baseURL': LOCAL}))
    mappings({f'{UPSTREAM}build/': export})

    register = json.loads(http_interceptor.load_content(f'{UPSTREAM}build/register.json'))

    assert register['baseURL'] == LOCAL


def test_base_url_without_trailing_slash_normalized(tmp_path, mappings):
    checkout = _checkout(tmp_path / 'upstream', LOCAL.rstrip('/'))
    (checkout / 'build-local' / 'annotated' / 'schema.json').write_text(
        json.dumps({'$ref': f'{LOCAL}build-local/annotated/other.json'}))
    mappings({UPSTREAM: checkout})

    schema = json.loads(http_interceptor.load_content(f'{UPSTREAM}build-local/annotated/schema.json'))

    assert schema['$ref'] == f'{UPSTREAM}build-local/annotated/other.json'


def test_non_text_files_untouched(tmp_path, mappings):
    checkout = _checkout(tmp_path / 'upstream', LOCAL)
    blob = b'\x89PNG\x00' + LOCAL.encode() + b'\x00'
    (checkout / 'build-local' / 'logo.png').write_bytes(blob)
    mappings({UPSTREAM: checkout})

    assert http_interceptor.load_content(f'{UPSTREAM}build-local/logo.png') == blob


def test_rebase_logged_once_per_upstream(tmp_path, mappings, caplog):
    mappings({UPSTREAM: _checkout(tmp_path / 'upstream', LOCAL)})

    with caplog.at_level('INFO', logger=http_interceptor.logger.name):
        http_interceptor.load_content(f'{UPSTREAM}build-local/register.json')
        http_interceptor.load_content(f'{UPSTREAM}build-local/annotated/schema.json')

    assert len([r for r in caplog.records if 'Rebasing local build' in r.message]) == 1
