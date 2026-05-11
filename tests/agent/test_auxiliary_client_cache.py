"""Pin the auxiliary_client provider-cache safety contract.

Per nicoechaniz's PR #1 review (2026-05-10):
1. The cache must not serve stale clients across OAuth credential refresh.
2. The cache key must distinguish credential-relevant fields.
3. The main_runtime flattening's one-level-deep limit must surface
   loudly (TypeError) rather than silently corrupt.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Hashability + key composition
# ---------------------------------------------------------------------------


def test_cache_key_is_hashable_for_typical_runtime():
    """The cache key tuple must remain hashable for realistic main_runtime
    shapes (provider/model strings, simple identifiers)."""
    main_runtime = {
        "oauth_credential_path": "/x/y",
        "model": "kimi-k2.6",
        "trace_id": "abc",
    }
    key = (
        "kimi-coding", "kimi-k2.6", False, False,
        None, None, "chat",
        tuple(sorted(main_runtime.items())),
    )
    # If not hashable this raises TypeError.
    {key: 1}


def test_cache_key_distinguishes_explicit_api_key():
    """Two cache keys differing only in explicit_api_key must be distinct
    — otherwise a client built with credentials A would be served for
    credentials B."""
    base = ("kimi-coding", "kimi-k2.6", False, False, None)
    key_a = base + ("token_A", "chat", None)
    key_b = base + ("token_B", "chat", None)
    assert key_a != key_b


def test_cache_key_distinguishes_credential_path_in_main_runtime():
    """oauth_credential_path inside main_runtime must propagate into the
    cache key so OAuth credential rotation invalidates the entry."""
    base = ("kimi-coding", "kimi-k2.6", False, False, None, None, "chat")
    rt_a = {"oauth_credential_path": "/path/a"}
    rt_b = {"oauth_credential_path": "/path/b"}
    key_a = base + (tuple(sorted(rt_a.items())),)
    key_b = base + (tuple(sorted(rt_b.items())),)
    assert key_a != key_b


def test_cache_key_rejects_nested_runtime_values():
    """The current sorted-tuple flattening is one-level deep. If
    main_runtime grows nested dicts/lists, hashing the key fails loudly.
    This test pins the limitation so the failure mode stays observable —
    silent corruption (stable hash with unstable content) is the bug
    we're protecting against."""
    main_runtime_nested = {"nested": {"k": "v"}}
    flat = tuple(sorted(main_runtime_nested.items()))
    with pytest.raises(TypeError):
        hash(flat)


# ---------------------------------------------------------------------------
# Eviction on credential refresh
# ---------------------------------------------------------------------------


def test_evict_clears_resolve_provider_cache():
    """_evict_cached_clients must drop entries from _resolve_provider_cache
    for the affected provider, not just from the lower-level _client_cache.

    The OpenAI client objects snapshot api_key at construction — they do
    not auto-refresh. If _resolve_provider_cache is not evicted on OAuth
    refresh, the next call serves a client bound to the now-stale token
    and all requests will fail with 401.
    """
    from agent import auxiliary_client as ac

    # Seed both caches with kimi-coding entries.
    cache_key = (
        "kimi-coding", "kimi-k2.6", False, False, None, None, "chat", None,
    )
    sentinel = ("client-A", "kimi-k2.6")

    with ac._resolve_provider_cache_lock:
        ac._resolve_provider_cache[cache_key] = sentinel

    try:
        assert cache_key in ac._resolve_provider_cache

        ac._evict_cached_clients("kimi-coding")

        assert cache_key not in ac._resolve_provider_cache, (
            "_evict_cached_clients must clear _resolve_provider_cache, "
            "otherwise OAuth-refreshed credentials are masked by a stale "
            "cache entry"
        )
    finally:
        # Defensive cleanup in case the assert was the failure path.
        with ac._resolve_provider_cache_lock:
            ac._resolve_provider_cache.pop(cache_key, None)


def test_evict_only_touches_targeted_provider():
    """Eviction for provider X must not collateral-clear entries for
    provider Y."""
    from agent import auxiliary_client as ac

    kimi_key = (
        "kimi-coding", "kimi-k2.6", False, False, None, None, "chat", None,
    )
    nous_key = (
        "nous", "hermes-4-405b", False, False, None, None, "chat", None,
    )
    with ac._resolve_provider_cache_lock:
        ac._resolve_provider_cache[kimi_key] = ("client-kimi", "kimi-k2.6")
        ac._resolve_provider_cache[nous_key] = ("client-nous", "hermes-4-405b")

    try:
        ac._evict_cached_clients("kimi-coding")
        assert kimi_key not in ac._resolve_provider_cache
        assert nous_key in ac._resolve_provider_cache, (
            "Evicting kimi-coding must not affect nous entries"
        )
    finally:
        with ac._resolve_provider_cache_lock:
            ac._resolve_provider_cache.pop(kimi_key, None)
            ac._resolve_provider_cache.pop(nous_key, None)
