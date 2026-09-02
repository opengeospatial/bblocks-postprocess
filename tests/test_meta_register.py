"""Tests for ogc.bblocks.meta_register's `@org/register` import alias resolution
(docs/meta-register-import-aliases.md)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from ogc.bblocks.meta_register import (
    MetaRegisterError,
    _cache_file,
    clear_cache,
    fetch_index,
    get_response,
    is_alias,
    resolve_imports,
    resolve_register_url,
)

MAIN_BBR = 'https://opengeospatial.github.io/bblocks/register.json'
DEFAULT_IMPORT_MARKER = 'default'
INDEX_URL = 'https://example.com/index.json'

INDEX_RESPONSE = {
    '@ogc/main': 'https://blocks.ogc.org/register.json',
    '@acme/foo': 'https://acme.example.com/register.json',
}


@pytest.fixture(autouse=True)
def clear_index_cache():
    # The index memo is process-lifetime; reset between tests so each test's
    # requests.get mock is actually exercised rather than serving a previous
    # test's memoized result.
    clear_cache()
    yield
    clear_cache()


def _mock_get(mocker, responses: dict[str, list]):
    """
    Mock requests.get with per-URL response queues (popped in order, so a URL
    requested twice in one test -- e.g. an index fetched, then force-refetched
    -- can return a different outcome each time). Each queued outcome is
    either an Exception (raised), a dict (a successful response whose .json()
    returns it -- for index.json fetches), or 'ok'/'fail' (a successful/
    failing reachability check -- for register.json verify GETs).
    """
    queues = {url: list(vals) for url, vals in responses.items()}

    def side_effect(url, *args, **kwargs):
        if url not in queues or not queues[url]:
            raise AssertionError(f"Unexpected/exhausted requests.get call for {url}")
        outcome = queues[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        resp = mocker.Mock()
        resp.raise_for_status = mocker.Mock()
        resp.ok = True
        if isinstance(outcome, dict):
            resp.json.return_value = outcome
        elif outcome == 'fail':
            resp.ok = False
            resp.raise_for_status.side_effect = requests.HTTPError('simulated failure')
        return resp

    return mocker.patch('requests.get', side_effect=side_effect)


def test_is_alias():
    assert is_alias('@ogc/main')
    assert not is_alias('https://example.com/register.json')


# --- resolve_register_url: the single entry point for resolving an alias (or
# the "default" marker) to a register.json URL, including the retry-on-a-
# refreshed-index behavior. ---

def test_resolve_register_url_success(mocker):
    register_url = INDEX_RESPONSE['@ogc/main']
    _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE], register_url: ['ok']})
    assert resolve_register_url('@ogc/main', INDEX_URL) == register_url


def test_resolve_register_url_verify_false_skips_reachability_check(mocker):
    register_url = INDEX_RESPONSE['@ogc/main']
    get = _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE]})
    assert resolve_register_url('@ogc/main', INDEX_URL, verify=False) == register_url
    get.assert_called_once()  # only the index fetch, no verify GET


def test_resolve_register_url_missing_key_raises_without_retry_when_index_fresh(mocker):
    get = _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE]})
    with pytest.raises(MetaRegisterError, match='@acme/unknown'):
        resolve_register_url('@acme/unknown', INDEX_URL)
    get.assert_called_once()  # no pointless second fetch: this index *was* just fetched fresh


def test_resolve_register_url_unreachable_register_raises_without_retry_when_index_fresh(mocker):
    register_url = INDEX_RESPONSE['@ogc/main']
    get = _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE], register_url: ['fail']})
    with pytest.raises(MetaRegisterError, match='not reachable'):
        resolve_register_url('@ogc/main', INDEX_URL)
    assert get.call_count == 2  # index once + verify once, no second index fetch


def test_resolve_register_url_retries_with_refreshed_index_after_stale_register_url(mocker):
    """The scenario this feature exists for: the in-process index is a
    (memoized, not just-fetched) cached copy whose register.json URL for this
    alias no longer works -- e.g. the register moved. Rather than failing
    outright, a fresh index is fetched and resolution retried once."""
    old_url = 'https://old.example.com/register.json'
    new_url = 'https://new.example.com/register.json'
    get = _mock_get(mocker, {
        INDEX_URL: [{'@ogc/main': old_url}, {'@ogc/main': new_url}],
        old_url: ['fail'],
        new_url: ['ok'],
    })
    fetch_index(INDEX_URL)  # warm the in-process memo, simulating an earlier call this run

    assert resolve_register_url('@ogc/main', INDEX_URL) == new_url
    assert get.call_count == 4  # old index (warm-up) + old url (fail) + new index (refresh) + new url (ok)


def test_resolve_register_url_gives_up_if_still_unreachable_after_refresh(mocker):
    register_url = 'https://old.example.com/register.json'
    get = _mock_get(mocker, {
        INDEX_URL: [{'@ogc/main': register_url}, {'@ogc/main': register_url}],
        register_url: ['fail', 'fail'],
    })
    fetch_index(INDEX_URL)  # warm the memo

    with pytest.raises(MetaRegisterError, match='not reachable'):
        resolve_register_url('@ogc/main', INDEX_URL)
    assert get.call_count == 4


def test_resolve_register_url_retries_with_refreshed_index_after_missing_key(mocker):
    """Same retry path, but the alias itself was missing from the cached
    index rather than its register.json being unreachable (e.g. it was added
    to the meta-registry after the cache was last populated)."""
    register_url = 'https://acme.example.com/register.json'
    get = _mock_get(mocker, {
        INDEX_URL: [{'@ogc/main': MAIN_BBR}, {'@ogc/main': MAIN_BBR, '@acme/foo': register_url}],
        register_url: ['ok'],
    })
    fetch_index(INDEX_URL)  # warm the memo with an index that doesn't have @acme/foo yet

    assert resolve_register_url('@acme/foo', INDEX_URL) == register_url
    assert get.call_count == 3


# --- get_response: shared in-process cache used by both resolve_register_url()'s
# verify step and (via meta_register.get_response, see models.py's load()) the
# actual subsequent fetch of the same register.json -- avoiding the duplicate
# request that would otherwise cost. ---

def test_get_response_caches_successful_response(mocker):
    get = _mock_get(mocker, {'https://example.com/register.json': ['ok']})
    r1 = get_response('https://example.com/register.json')
    r2 = get_response('https://example.com/register.json')
    assert r1 is r2
    get.assert_called_once()


def test_get_response_does_not_cache_failed_response(mocker):
    get = _mock_get(mocker, {'https://example.com/register.json': ['fail', 'ok']})
    r1 = get_response('https://example.com/register.json')
    assert not r1.ok
    r2 = get_response('https://example.com/register.json')
    assert r2.ok
    assert get.call_count == 2


def test_verify_and_a_subsequent_load_share_one_request(mocker):
    """The actual scenario this exists for: resolve_register_url()'s verify
    GET and models.py's ImportedBuildingBlocks.load() (which now also calls
    meta_register.get_response()) fetching the very same URL moments apart
    within one run should only hit the network once between them."""
    register_url = INDEX_RESPONSE['@ogc/main']
    get = _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE], register_url: ['ok']})

    resolve_register_url('@ogc/main', INDEX_URL)  # does the verify GET
    get_response(register_url)  # what ImportedBuildingBlocks.load() would do next

    assert get.call_count == 2  # index + the one shared register.json fetch, not two
# --- resolve_imports: entrypoint.py's bblocks-config.yaml `imports` handling. ---

def test_missing_imports_key_defaults_to_main_register(mocker):
    get = mocker.patch('requests.get')
    assert resolve_imports(None, None, DEFAULT_IMPORT_MARKER, MAIN_BBR) == [MAIN_BBR]
    get.assert_not_called()


def test_default_marker_substituted_with_main_register(mocker):
    get = mocker.patch('requests.get')
    assert resolve_imports(['default'], None, DEFAULT_IMPORT_MARKER, MAIN_BBR) == [MAIN_BBR]
    get.assert_not_called()


def test_raw_url_imports_pass_through_unchanged(mocker):
    get = mocker.patch('requests.get')
    imports = ['https://example.com/register.json', 'https://other.example.com/register.json']
    assert resolve_imports(imports, None, DEFAULT_IMPORT_MARKER, MAIN_BBR) == imports
    get.assert_not_called()


def test_falsy_import_entries_filtered_out(mocker):
    get = mocker.patch('requests.get')
    imports = ['https://example.com/register.json', '', None]
    assert resolve_imports(imports, None, DEFAULT_IMPORT_MARKER, MAIN_BBR) == \
        ['https://example.com/register.json']
    get.assert_not_called()


def test_missing_alias_raises_clear_error(mocker):
    _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE]})
    with pytest.raises(MetaRegisterError, match='@acme/unknown'):
        resolve_imports(['@acme/unknown'], INDEX_URL, DEFAULT_IMPORT_MARKER, MAIN_BBR)


def test_alias_without_meta_registry_configured_raises_clear_error(mocker):
    get = mocker.patch('requests.get')
    with pytest.raises(MetaRegisterError, match='no meta-registry is configured'):
        resolve_imports(['@ogc/main'], None, DEFAULT_IMPORT_MARKER, MAIN_BBR)
    get.assert_not_called()


def test_default_marker_mixed_with_raw_urls_and_aliases(mocker):
    acme_url = INDEX_RESPONSE['@acme/foo']
    _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE], acme_url: ['ok']})

    result = resolve_imports(
        ['default', 'https://raw.example.com/register.json', '@acme/foo'],
        INDEX_URL, DEFAULT_IMPORT_MARKER, MAIN_BBR,
    )
    # INDEX_RESPONSE has no "default" key -> falls back to MAIN_BBR.
    assert result == [MAIN_BBR, 'https://raw.example.com/register.json', acme_url]


# --- "default" marker vs. the meta-registry's own "default" index entry:
# the index carries a "default" key by design (kept in sync with whatever it
# considers the main register); resolve_imports prefers that when reachable,
# falling back to the local main_register_url constant on any failure so
# resolving "default" never becomes a hard network dependency. ---

def test_default_marker_prefers_meta_registry_entry_over_local_constant(mocker):
    # Index's "default" deliberately differs from the local MAIN_BBR constant,
    # to prove the meta-registry value wins when both are available.
    from_index_url = 'https://blocks.ogc.org/from-index.json'
    _mock_get(mocker, {INDEX_URL: [{'default': from_index_url}], from_index_url: ['ok']})

    result = resolve_imports(['default'], INDEX_URL, DEFAULT_IMPORT_MARKER, MAIN_BBR)
    assert result == [from_index_url]


def test_default_marker_falls_back_to_local_constant_when_meta_registry_unreachable(mocker):
    mocker.patch('requests.get', side_effect=requests.ConnectionError('boom'))
    result = resolve_imports(['default'], INDEX_URL, DEFAULT_IMPORT_MARKER, MAIN_BBR)
    assert result == [MAIN_BBR]


def test_default_marker_falls_back_to_local_constant_when_index_lacks_default_key(mocker):
    _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE]})  # no "default" key
    result = resolve_imports(['default'], INDEX_URL, DEFAULT_IMPORT_MARKER, MAIN_BBR)
    assert result == [MAIN_BBR]


def test_missing_imports_key_also_prefers_meta_registry_default_when_configured(mocker):
    from_index_url = 'https://blocks.ogc.org/from-index.json'
    _mock_get(mocker, {INDEX_URL: [{'default': from_index_url}], from_index_url: ['ok']})

    result = resolve_imports(None, INDEX_URL, DEFAULT_IMPORT_MARKER, MAIN_BBR)
    assert result == [from_index_url]


# --- On-disk caching (store index.json for at least a few days, for local/
# offline-friendly runs). ---

def _write_test_cache(cache_dir, url, index, age):
    cache_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc) - age
    with open(_cache_file(cache_dir, url), 'w') as f:
        json.dump({'fetched_at': fetched_at.isoformat(), 'index': index}, f)


def test_fresh_cache_used_without_any_network_call(mocker, tmp_path):
    get = mocker.patch('requests.get')
    _write_test_cache(tmp_path, INDEX_URL, INDEX_RESPONSE, age=timedelta(hours=1))

    result = fetch_index(INDEX_URL, cache_dir=tmp_path, ttl_days=3)
    assert result == INDEX_RESPONSE
    get.assert_not_called()


def test_stale_cache_triggers_refetch_and_is_overwritten(mocker, tmp_path):
    _write_test_cache(tmp_path, INDEX_URL, {'@old/entry': 'https://old.example.com/register.json'},
                      age=timedelta(days=10))
    get = _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE]})

    result = fetch_index(INDEX_URL, cache_dir=tmp_path, ttl_days=3)
    assert result == INDEX_RESPONSE
    get.assert_called_once()

    with open(_cache_file(tmp_path, INDEX_URL)) as f:
        assert json.load(f)['index'] == INDEX_RESPONSE


def test_successful_fetch_writes_cache_for_next_run(mocker, tmp_path):
    _mock_get(mocker, {INDEX_URL: [INDEX_RESPONSE]})
    fetch_index(INDEX_URL, cache_dir=tmp_path, ttl_days=3)

    cache_file = _cache_file(tmp_path, INDEX_URL)
    assert cache_file.is_file()
    with open(cache_file) as f:
        assert json.load(f)['index'] == INDEX_RESPONSE


def test_network_failure_falls_back_to_stale_cache_instead_of_raising(mocker, tmp_path):
    _write_test_cache(tmp_path, INDEX_URL, INDEX_RESPONSE, age=timedelta(days=30))
    mocker.patch('requests.get', side_effect=requests.ConnectionError('boom'))

    result = fetch_index(INDEX_URL, cache_dir=tmp_path, ttl_days=3)
    assert result == INDEX_RESPONSE


def test_network_failure_without_any_cache_still_raises(mocker, tmp_path):
    mocker.patch('requests.get', side_effect=requests.ConnectionError('boom'))
    with pytest.raises(MetaRegisterError, match='Could not fetch meta-registry index'):
        fetch_index(INDEX_URL, cache_dir=tmp_path, ttl_days=3)
