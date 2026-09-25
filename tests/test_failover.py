"""
Unit tests for the PriorityFailoverProvider (mindshub-free first, typesafe-pool fallback).
"""

import time

import httpx
import pytest

from decidex.pool import KeyPool
from decidex.providers import failover as failover_mod
from decidex.providers.base import BaseInferenceProvider
from decidex.providers.failover import (
    NoAvailableProvidersError,
    PriorityFailoverProvider,
)
from decidex.providers.typesafe import TypeSafeJevProvider
from decidex.types import ObservationPayload, PrimitiveType, QuestionSpec


def _verdict(value):
    return [
        {
            "id": "q1",
            "primitive": PrimitiveType.CHOICE,
            "selected": value,
            "raw_confidence": 0.9,
        }
    ]


class StaticProvider(BaseInferenceProvider):
    """Provider stub that either returns canned verdicts or raises a canned exception."""

    def __init__(self, verdicts=None, exc=None):
        self.verdicts = verdicts or []
        self.exc = exc
        self.calls = 0

    async def infer(self, payload, questions):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return [_StubVerdict(v) for v in self.verdicts]


class _StubVerdict:
    def __init__(self, data):
        self.__dict__.update(data)


def _http_failure(status, headers=None, reason="client error"):
    """Build a RuntimeError chain that mimics TypeSafeJevProvider surfacing an HTTP error."""
    req = httpx.Request("POST", "https://example.invalid/v1/decisions")
    resp = httpx.Response(status, request=req, headers=headers or {}, json={"error": reason})
    exc = RuntimeError(f"TypeSafeJevProvider request failed: {reason}")
    exc.__cause__ = httpx.HTTPStatusError(reason, request=req, response=resp)
    return exc


def _questions():
    return [QuestionSpec(id="q1", primitive=PrimitiveType.CHOICE, description="t", options=["A", "B"])]


def _payload():
    return ObservationPayload(domain="test", observation={})


@pytest.mark.asyncio
async def test_primary_serves_secondary_untouched():
    primary = StaticProvider(verdicts=_verdict("A"))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("primary", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())

    assert verdicts[0].selected == "A"
    assert primary.calls == 1 and secondary.calls == 0
    stats = provider.stats()
    assert stats["routes"][0]["state"] == "active"
    assert stats["routes"][1]["total_requests"] == 0


@pytest.mark.asyncio
async def test_failover_on_429_honors_retry_after():
    primary = StaticProvider(exc=_http_failure(429, headers={"Retry-After": "7"}))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("primary", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())

    assert verdicts[0].selected == "B"
    route = provider.stats()["routes"][0]
    assert route["state"] == "cooldown"
    assert 6.0 <= route["cooldown_remaining_s"] <= 7.5
    assert "429" in route["last_error"]

    # While the primary is cooling, the next call skips straight to the secondary
    await provider.infer(_payload(), _questions())
    assert primary.calls == 1 and secondary.calls == 2


@pytest.mark.asyncio
async def test_failover_on_402_marks_route_dead():
    primary = StaticProvider(exc=_http_failure(402, reason="wallet_empty"))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("primary", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "B"

    # Dead routes are never retried
    await provider.infer(_payload(), _questions())
    assert primary.calls == 1 and secondary.calls == 2
    assert provider.stats()["routes"][0]["state"] == "dead"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 422])
async def test_route_key_level_4xx_cooldowns_then_recovers(status):
    """One key's 403 (risk control) or a single rejected payload must never kill a pool route."""
    primary = StaticProvider(exc=_http_failure(status))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider(
        [("primary", primary), ("secondary", secondary)], default_cooldown_s=0.1
    )

    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "B"
    route = provider.stats()["routes"][0]
    assert route["state"] == "cooldown"
    assert f"HTTP {status}" in route["last_error"]

    # Cooldown expiry: the primary route is retried and recovers on success
    time.sleep(0.15)
    primary.exc = None
    primary.verdicts = _verdict("A")
    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "A"
    assert primary.calls == 2
    assert provider.stats()["routes"][0]["state"] == "active"


@pytest.mark.asyncio
async def test_deterministic_404_marks_route_dead():
    provider = PriorityFailoverProvider([
        ("primary", StaticProvider(exc=_http_failure(404))),
        ("secondary", StaticProvider(verdicts=_verdict("B"))),
    ])
    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "B"
    assert provider.stats()["routes"][0]["state"] == "dead"


@pytest.mark.asyncio
async def test_inner_pool_exhaustion_cooldowns_but_never_dies():
    primary = StaticProvider(
        exc=RuntimeError("TypeSafeJevProvider exhausted all available keys: all 3 keys cooling down")
    )
    provider = PriorityFailoverProvider([("primary", primary)])

    with pytest.raises(NoAvailableProvidersError):
        await provider.infer(_payload(), _questions())

    route = provider.stats()["routes"][0]
    assert route["state"] == "cooldown"
    assert "pool exhausted" in route["last_error"]


@pytest.mark.asyncio
async def test_all_routes_unavailable_raises():
    provider = PriorityFailoverProvider([
        ("a", StaticProvider(exc=_http_failure(429))),
        ("b", StaticProvider(exc=_http_failure(402))),
    ])
    with pytest.raises(NoAvailableProvidersError):
        await provider.infer(_payload(), _questions())
    stats = provider.stats()
    assert stats["cooldown_routes"] == 1 and stats["dead_routes"] == 1


@pytest.mark.asyncio
async def test_cooldown_expiry_recovers_priority():
    provider = PriorityFailoverProvider([("primary", StaticProvider(verdicts=_verdict("A")))])
    provider.routes[0].mark_cooldown(0.1, "test cooldown")
    time.sleep(0.15)

    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "A"
    assert provider.stats()["routes"][0]["state"] == "active"


@pytest.mark.asyncio
async def test_aclose_closes_inner_providers():
    inner = TypeSafeJevProvider(api_keys=["k1"])
    provider = PriorityFailoverProvider([("inner", inner)])
    await provider.aclose()
    assert inner._client is None or inner._client.is_closed


def test_from_default_routes_splits_env_and_tagged_keys(tmp_path, monkeypatch):
    key_file = tmp_path / "keys.jsonl"
    key_file.write_text(
        '{"key": "mdb_file_key", "org": "mindshub"}\n'
        '{"key": "apikey_official_1"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MINDSHUB_API_KEY", "mdb_env_key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DECIDEX_API_KEY", raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [str(key_file)])

    provider = PriorityFailoverProvider.from_default_routes()

    assert [r.name for r in provider.routes] == ["mindshub-free", "typesafe-pool"]
    mindshub_route, typesafe_route = provider.routes

    assert isinstance(mindshub_route.provider, TypeSafeJevProvider)
    assert mindshub_route.provider.base_url == "https://api.mindshub.ai/v1/decisions"
    assert mindshub_route.provider.model == "jev"
    assert mindshub_route.provider.max_retries == 0
    assert set(provider.routes[0].provider.key_pool._key_map) == {"mdb_env_key", "mdb_file_key"}

    assert isinstance(typesafe_route.provider, TypeSafeJevProvider)
    assert typesafe_route.provider.base_url == "https://api.typesafe.ai/v1"
    assert set(provider.routes[1].provider.key_pool._key_map) == {"apikey_official_1"}


def test_from_default_routes_mindshub_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSHUB_API_KEY", "mdb_only")
    for var in ("TYPESAFE_API_KEY", "DECIDEX_API_KEY", "DECIDEX_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [])

    provider = PriorityFailoverProvider.from_default_routes()

    assert len(provider.routes) == 1
    assert provider.routes[0].name == "mindshub-free"


def test_from_default_routes_without_credentials_raises(tmp_path, monkeypatch):
    for var in ("MINDSHUB_API_KEY", "TYPESAFE_API_KEY", "DECIDEX_API_KEY", "DECIDEX_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [])

    with pytest.raises(NoAvailableProvidersError):
        PriorityFailoverProvider.from_default_routes()


def test_from_default_routes_uses_pool_candidate_sources(monkeypatch):
    """failover must reuse KeyPool's candidate list (single source of truth)."""
    monkeypatch.delenv("DECIDEX_KEY_FILE", raising=False)
    assert failover_mod._key_source_candidates() == (
        KeyPool.default_key_file_candidates() + KeyPool.vault_key_note_candidates()
    )
