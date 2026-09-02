#!/usr/bin/env python3
"""Resolution of `@org/register` import aliases against a Building Blocks
meta-registry (see ../bblocks-meta-register).

An `imports` entry in `bblocks-config.yaml` may be written as `@org/register`
instead of a raw register.json URL. This module resolves such aliases to
URLs by fetching the meta-registry's compiled `index.json` -- a flat
`{"@org/register": "https://.../register.json", ...}` map, published to
GitHub Pages from ../bblocks-meta-register-data -- and looking them up
there.

This is purely additive: entries that don't start with `@` are returned
unchanged, and the index is only fetched at all if at least one alias is
present in the imports list.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Stable w3id.org front for the compiled index published by
# ../bblocks-meta-register-data to GitHub Pages (currently redirects there;
# the redirect target can change without affecting this default). Served
# with ETag / Cache-Control: max-age=600 by GitHub Pages' own CDN, so
# repeated fetches in a short window are already cheap without any caching
# logic on our side.
DEFAULT_META_REGISTRY_URL: str | None = 'https://w3id.org/ogc/bblocks/meta-register.json'

# How long a locally cached copy of the index is trusted before a fresh fetch
# is attempted. Deliberately generous (days, not minutes/hours): the index
# changes rarely, and this cache exists mainly so a local/interactive run
# doesn't need network access on every invocation -- not to shadow genuinely
# fresh CDN-served data (GitHub Pages already handles that with its own
# short-lived Cache-Control).
DEFAULT_CACHE_TTL_DAYS = 3


class MetaRegisterError(Exception):
    """Raised when an `@org/register` import alias cannot be resolved."""


def is_alias(entry: str) -> bool:
    return entry.startswith('@')


def _cache_file(cache_dir: Path, meta_registry_url: str) -> Path:
    url_hash = sha256(meta_registry_url.encode('utf-8')).hexdigest()
    return cache_dir / f"{url_hash}.json"


def _read_cache(cache_file: Path) -> tuple[dict, datetime] | None:
    if not cache_file.is_file():
        return None
    try:
        with open(cache_file) as f:
            payload = json.load(f)
        return payload['index'], datetime.fromisoformat(payload['fetched_at'])
    except Exception as e:
        logger.warning("Ignoring unreadable meta-registry index cache %s: %s", cache_file, e)
        return None


def _write_cache(cache_file: Path, index: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, 'w') as f:
            json.dump({'fetched_at': datetime.now(timezone.utc).isoformat(), 'index': index}, f)
    except Exception as e:
        logger.warning("Could not write meta-registry index cache to %s: %s", cache_file, e)


# In-process memo, keyed by (url, cache_dir, ttl_days) -- a manual dict rather
# than functools.lru_cache so a force_refresh=True call (see
# _fetch_index_with_origin) can update the same slot that plain calls read,
# instead of leaving a separate, now-stale, lru_cache entry behind for them.
_index_memo: dict[tuple[str, Path | None, float], dict] = {}


def clear_cache() -> None:
    """Test helper: drop the in-process index memo and response cache."""
    _index_memo.clear()
    _response_cache.clear()


def _fetch_index_with_origin(meta_registry_url: str, cache_dir: Path | None = None,
                              ttl_days: float = DEFAULT_CACHE_TTL_DAYS,
                              force_refresh: bool = False) -> tuple[dict[str, str], bool]:
    """
    Fetch and parse the meta-registry's compiled index.json, cache-first: the
    in-process memo, then an on-disk copy (if `cache_dir` is given and it's
    fresh enough per `ttl_days`), then the network -- unless `force_refresh`,
    which skips straight to the network.

    On a network failure (or a malformed response), a stale on-disk copy --
    however old -- is used as a last resort, with a warning, rather than
    failing the whole run; this is meant for local/offline-friendly runs,
    not to mask a genuinely broken meta-registry in CI (where no cache_dir
    is typically persisted across invocations anyway).

    Returns `(index, from_network)`: `from_network` is True only when this
    call actually hit the network successfully just now -- resolve_register_url()
    uses it to decide whether refreshing and retrying could possibly help
    (no point refreshing again if the index we have *is* already the latest).
    """
    memo_key = (meta_registry_url, cache_dir, ttl_days)
    if not force_refresh and memo_key in _index_memo:
        return _index_memo[memo_key], False

    cache_file = _cache_file(cache_dir, meta_registry_url) if cache_dir else None

    if cache_file and not force_refresh:
        cached = _read_cache(cache_file)
        if cached is not None:
            index, fetched_at = cached
            if datetime.now(timezone.utc) - fetched_at <= timedelta(days=ttl_days):
                _index_memo[memo_key] = index
                return index, False

    fetch_error: Exception | None = None
    index: dict | None = None
    try:
        r = requests.get(meta_registry_url)
        r.raise_for_status()
        index = r.json()
        if not isinstance(index, dict):
            raise ValueError(f"Meta-registry index at {meta_registry_url} is not a JSON object")
    except Exception as e:
        fetch_error = e

    if fetch_error is None:
        if cache_file:
            _write_cache(cache_file, index)
        _index_memo[memo_key] = index
        return index, True

    if cache_file:
        stale = _read_cache(cache_file)
        if stale is not None:
            logger.warning(
                "Could not fetch meta-registry index from %s (%s); using cached copy from %s",
                meta_registry_url, fetch_error, stale[1].isoformat(),
            )
            _index_memo[memo_key] = stale[0]
            return stale[0], False

    raise MetaRegisterError(
        f"Could not fetch meta-registry index from {meta_registry_url}: {fetch_error}"
    ) from fetch_error


def fetch_index(meta_registry_url: str, cache_dir: Path | None = None,
                 ttl_days: float = DEFAULT_CACHE_TTL_DAYS) -> dict[str, str]:
    """Fetch and parse the meta-registry's compiled index.json. See
    _fetch_index_with_origin() for the cache-first fetch strategy."""
    index, _ = _fetch_index_with_origin(meta_registry_url, cache_dir, ttl_days)
    return index


# Shared in-process cache of fetched register.json responses, keyed by URL.
# Process-lifetime only -- deliberately NOT persisted to disk like the index
# cache, since register content changes far more often than the meta-registry
# index does. Its only purpose is to let resolve_register_url()'s reachability
# check and a subsequent real load of the same URL (typically
# ImportedBuildingBlocks.load(), via get_response() -- models.py is the one
# other place that imports this module, precisely to share this cache) avoid
# fetching the same register.json twice.
_response_cache: dict[str, requests.Response] = {}


def get_response(url: str) -> requests.Response:
    """
    GET `url`, cached in-process for the rest of this run -- but only a
    successful (2xx) response. A failure (network-level, or an HTTP error
    status) is deliberately never cached: caching it would mean a retry
    against the very same URL -- e.g. resolve_register_url()'s
    refresh-and-retry, when the refreshed index still points at the same
    URL because the register is genuinely down rather than moved -- could
    never observe a transient failure clearing up, since it'd just keep
    replaying the first failure instead of trying again.
    """
    if url in _response_cache:
        return _response_cache[url]
    r = requests.get(url)
    if r.ok:
        _response_cache[url] = r
    return r


def _try_resolve(lookup_key: str, index: dict[str, str], verify: bool) -> tuple[str | None, str | None]:
    """Look `lookup_key` up in `index` and, if `verify`, confirm the resulting
    URL is actually reachable with a GET. Returns `(url, error)` -- exactly
    one of which is None."""
    url = index.get(lookup_key)
    if not url:
        return None, f"{lookup_key!r} not found in meta-registry index"
    if verify:
        try:
            r = get_response(url)
            r.raise_for_status()
        except Exception as e:
            return None, f"register.json at {url} (for {lookup_key!r}) is not reachable: {e}"
    return url, None


def resolve_register_url(lookup_key: str, meta_registry_url: str, cache_dir: Path | None = None,
                          ttl_days: float = DEFAULT_CACHE_TTL_DAYS, verify: bool = True) -> str:
    """
    Resolve `lookup_key` -- an `@org/register` alias, or the `"default"`
    marker -- to a register.json URL via the meta-registry: single entry
    point for loading a register by alias, used wherever one is needed.

    Cache-first (in-process memo, then on-disk TTL cache, then network) via
    _fetch_index_with_origin(). If resolution fails -- the key isn't in the
    index, or (when `verify`) the resolved register.json isn't actually
    reachable -- **and** the index used wasn't itself just freshly fetched
    from the network this call, the index is force-refreshed (bypassing the
    TTL cache, since that cache is presumably what produced the failure) and
    resolution is retried once. This recovers from a register that has moved
    since the local index was last refreshed, without waiting out the TTL.

    Raises MetaRegisterError if resolution still fails after that retry (or
    immediately, if the index was already fresh).
    """
    index, from_network = _fetch_index_with_origin(meta_registry_url, cache_dir, ttl_days)
    url, error = _try_resolve(lookup_key, index, verify)

    if error and not from_network:
        logger.info("Could not resolve %r (%s); refreshing meta-registry index and retrying",
                   lookup_key, error)
        index, _ = _fetch_index_with_origin(meta_registry_url, cache_dir, ttl_days, force_refresh=True)
        url, error = _try_resolve(lookup_key, index, verify)

    if error:
        raise MetaRegisterError(
            f"Could not resolve {lookup_key!r} via meta-registry ({meta_registry_url}): {error}"
        )
    return url


def resolve_imports(imports_raw: list[str] | None, meta_registry_url: str | None,
                     default_marker: str, main_register_url: str,
                     cache_dir: Path | None = None,
                     ttl_days: float = DEFAULT_CACHE_TTL_DAYS) -> list[str]:
    """
    Full resolution of a bblocks-config.yaml `imports` value into a list of
    register.json URLs.

    - A missing/null `imports_raw` is treated as `[default_marker]`.
    - `default_marker` entries resolve via resolve_register_url() when a
      meta-registry is configured, falling back to the local
      `main_register_url` constant on *any* failure (unconfigured,
      unreachable even after retry, key missing) -- this must never turn the
      common "no imports configured at all" case into a hard requirement,
      so it degrades silently rather than raising.
    - `@org/register` aliases resolve via resolve_register_url() too, but
      raise MetaRegisterError on failure -- there's no safe local fallback
      for an alias the author explicitly asked for.
    - Anything else is treated as a raw URL and passed through unchanged,
      with no meta-registry involvement (and so no network access) at all.

    Pulled out of entrypoint.py as a pure function so this -- especially the
    backward-compatible cases where no alias is involved at all -- can be
    unit-tested directly; entrypoint.py itself is flat script code with no
    test coverage of its own.
    """
    if imports_raw is None:
        imports_raw = [default_marker]

    resolved = []
    for entry in imports_raw:
        if not entry:
            continue
        if entry == default_marker:
            if meta_registry_url:
                try:
                    resolved.append(resolve_register_url(default_marker, meta_registry_url, cache_dir, ttl_days))
                    continue
                except MetaRegisterError as e:
                    logger.info("Could not resolve %r via meta-registry, falling back to %s: %s",
                               default_marker, main_register_url, e)
            resolved.append(main_register_url)
        elif is_alias(entry):
            if not meta_registry_url:
                raise MetaRegisterError(
                    f"Import alias {entry!r} used but no meta-registry is configured "
                    f"(set 'meta-registry' in bblocks-config.yaml)"
                )
            url = resolve_register_url(entry, meta_registry_url, cache_dir, ttl_days)
            logger.info("Resolved import alias %s to %s", entry, url)
            resolved.append(url)
        else:
            resolved.append(entry)
    return resolved
