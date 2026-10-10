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
async def test_failover_on_insufficient_user_quota_marks_route_dead():
    req = httpx.Request("POST", "https://example.invalid/v1/systemone")
    resp = httpx.Response(
        403,
        request=req,
        json={"error": {"code": "insufficient_user_quota", "message": "Balance zero"}},
    )
    status_err = httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url 'https://example.invalid/v1/systemone'",
        request=req,
        response=resp,
    )
    exc = RuntimeError("TypeSafeJevProvider request failed after 1 attempts: Client error '403 Forbidden'")
    exc.__cause__ = status_err

    primary = StaticProvider(exc=exc)
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("primary", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "B"
    assert provider.stats()["routes"][0]["state"] == "dead"
    assert "quota/wallet exhausted" in provider.stats()["routes"][0]["last_error"]


@pytest.mark.asyncio
async def test_three_tier_failover_cascade():
    """FuturePPO (P1) rate-limits -> MindsHub (P2) quota exhausted -> TypeSafe (P3) succeeds."""
    r1 = StaticProvider(exc=_http_failure(429, headers={"Retry-After": "30"}))
    r2 = StaticProvider(exc=_http_failure(402, reason="insufficient_user_quota"))
    r3 = StaticProvider(verdicts=_verdict("C"))
    provider = PriorityFailoverProvider([
        ("futureppo-pool", r1),
        ("mindshub-free", r2),
        ("typesafe-pool", r3),
    ])

    verdicts = await provider.infer(_payload(), _questions())
    assert verdicts[0].selected == "C"
    assert r1.calls == 1 and r2.calls == 1 and r3.calls == 1

    stats = provider.stats()
    assert stats["routes"][0]["state"] == "cooldown"
    assert stats["routes"][1]["state"] == "dead"
    assert stats["routes"][2]["state"] == "active"

    # Second infer skips P1 (cooldown) and P2 (dead), routes straight to P3
    verdicts2 = await provider.infer(_payload(), _questions())
    assert verdicts2[0].selected == "C"
    assert r1.calls == 1 and r2.calls == 1 and r3.calls == 2


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
        '{"key": "fpp_file_key", "org": "futureppo"}\n'
        '{"key": "mdb_file_key", "org": "mindshub"}\n'
        '{"key": "apikey_official_1"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FUTUREPPO_API_KEY", "fpp_env_key")
    monkeypatch.setenv("MINDSHUB_API_KEY", "mdb_env_key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DECIDEX_API_KEY", raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [str(key_file)])

    provider = PriorityFailoverProvider.from_default_routes()

    assert [r.name for r in provider.routes] == ["futureppo-pool", "mindshub-free", "typesafe-pool"]
    fpp_route, mindshub_route, typesafe_route = provider.routes

    assert isinstance(fpp_route.provider, TypeSafeJevProvider)
    assert fpp_route.provider.base_url == "https://pool.futureppo.top/v1/systemone"
    assert fpp_route.provider.model == "jev-latest"
    assert fpp_route.provider.max_retries == 0
    assert set(provider.routes[0].provider.key_pool._key_map) == {"fpp_env_key", "fpp_file_key"}

    assert isinstance(mindshub_route.provider, TypeSafeJevProvider)
    assert mindshub_route.provider.base_url == "https://api.mindshub.ai/v1/decisions"
    assert mindshub_route.provider.model == "jev"
    assert mindshub_route.provider.max_retries == 0
    assert set(provider.routes[1].provider.key_pool._key_map) == {"mdb_env_key", "mdb_file_key"}

    assert isinstance(typesafe_route.provider, TypeSafeJevProvider)
    assert typesafe_route.provider.base_url == "https://api.typesafe.ai/v1"
    assert set(provider.routes[2].provider.key_pool._key_map) == {"apikey_official_1"}


def test_from_default_routes_futureppo_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FUTUREPPO_API_KEY", "fpp_only")
    for var in ("DECIDEX_FUTUREPPO_KEY", "MINDSHUB_API_KEY", "TYPESAFE_API_KEY", "DECIDEX_API_KEY", "DECIDEX_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [])

    provider = PriorityFailoverProvider.from_default_routes()
    assert len(provider.routes) == 1
    assert provider.routes[0].name == "futureppo-pool"
    assert "fpp_only" in provider.routes[0].provider.key_pool._key_map


def test_from_default_routes_mindshub_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDSHUB_API_KEY", "mdb_only")
    for var in ("FUTUREPPO_API_KEY", "DECIDEX_FUTUREPPO_KEY", "TYPESAFE_API_KEY", "DECIDEX_API_KEY", "DECIDEX_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [])

    provider = PriorityFailoverProvider.from_default_routes()

    assert len(provider.routes) == 1
    assert provider.routes[0].name == "mindshub-free"


def test_from_default_routes_without_credentials_raises(tmp_path, monkeypatch):
    for var in ("FUTUREPPO_API_KEY", "DECIDEX_FUTUREPPO_KEY", "MINDSHUB_API_KEY", "TYPESAFE_API_KEY", "DECIDEX_API_KEY", "DECIDEX_KEY_FILE"):
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


@pytest.mark.asyncio
async def test_failover_on_429_honors_retry_after_ms():
    """Nace Drex reports cooldowns as retry-after-ms on 429."""
    primary = StaticProvider(exc=_http_failure(429, headers={"retry-after-ms": "3000"}))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("nace", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())

    assert verdicts[0].selected == "B"
    route = provider.stats()["routes"][0]
    assert route["state"] == "cooldown"
    assert 2.5 <= route["cooldown_remaining_s"] <= 3.5


@pytest.mark.asyncio
async def test_retry_after_ms_used_when_retry_after_is_not_numeric():
    """A non-numeric Retry-After (HTTP-date) must not suppress the ms fallback."""
    primary = StaticProvider(exc=_http_failure(
        429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT", "retry-after-ms": "4000"}
    ))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("nace", primary), ("secondary", secondary)])

    await provider.infer(_payload(), _questions())

    route = provider.stats()["routes"][0]
    assert route["state"] == "cooldown"
    assert 3.5 <= route["cooldown_remaining_s"] <= 4.5


@pytest.mark.asyncio
async def test_failover_on_529_capacity_cools_down():
    """Nace returns 529 "at capacity" when the shared pool is full; treat as transient."""
    primary = StaticProvider(exc=_http_failure(529, headers={"retry-after-ms": "2000"}, reason="at capacity"))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("nace", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())

    assert verdicts[0].selected == "B"
    route = provider.stats()["routes"][0]
    assert route["state"] == "cooldown"
    assert "529" in route["last_error"]
    assert 1.5 <= route["cooldown_remaining_s"] <= 2.5


@pytest.mark.asyncio
async def test_failover_on_503_stays_on_generic_transient_path():
    """Generic 503 must NOT cool a whole route down on the first hit (only 529 does);
    it accumulates until max_consecutive_failures."""
    primary = StaticProvider(exc=_http_failure(503))
    secondary = StaticProvider(verdicts=_verdict("B"))
    provider = PriorityFailoverProvider([("typesafe", primary), ("secondary", secondary)])

    verdicts = await provider.infer(_payload(), _questions())

    assert verdicts[0].selected == "B"
    # A single 503 is transient: route stays active (not cooled down).
    assert provider.stats()["routes"][0]["state"] == "active"


def test_from_default_routes_non_gateway_tags_belong_to_official_pool(tmp_path, monkeypatch):
    """Only known gateway orgs (GATEWAY_ORGS) are diverted. Any other tag -- a
    rollout/infra label on an official key -- must land in the official pool, not
    be silently dropped."""
    key_file = tmp_path / "keys.jsonl"
    key_file.write_text(
        '{"key": "nace_file_key", "org": "nace"}\n'
        '{"key": "pxed_rollout_key", "org": "pxed"}\n'
        '{"key": "apikey_official_1"}\n',
        encoding="utf-8",
    )
    for var in ("NACE_API_KEY", "DREX_API_KEY", "MINDSHUB_API_KEY", "TYPESAFE_API_KEY",
                "DECIDEX_API_KEY", "FUTUREPPO_API_KEY", "DECIDEX_FUTUREPPO_KEY", "DECIDEX_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [str(key_file)])

    provider = PriorityFailoverProvider.from_default_routes()

    assert [r.name for r in provider.routes] == ["nace-decision", "typesafe-pool"]
    assert set(provider.routes[0].provider.key_pool._key_map) == {"nace_file_key"}
    assert set(provider.routes[1].provider.key_pool._key_map) == {"pxed_rollout_key", "apikey_official_1"}


def test_from_default_routes_includes_nace_route(tmp_path, monkeypatch):
    key_file = tmp_path / "keys.jsonl"
    key_file.write_text(
        '{"key": "nace_file_key", "org": "nace"}\n'
        '{"key": "mdb_file_key", "org": "mindshub"}\n'
        '{"key": "apikey_official_1"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DREX_API_KEY", "nace_env_key")
    for var in ("NACE_API_KEY", "MINDSHUB_API_KEY", "TYPESAFE_API_KEY", "DECIDEX_API_KEY",
                "FUTUREPPO_API_KEY", "DECIDEX_FUTUREPPO_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(failover_mod, "_key_source_candidates", lambda: [str(key_file)])

    provider = PriorityFailoverProvider.from_default_routes()

    assert [r.name for r in provider.routes] == ["nace-decision", "mindshub-free", "typesafe-pool"]

    nace_route = provider.routes[0]
    assert isinstance(nace_route.provider, TypeSafeJevProvider)
    assert nace_route.provider.base_url == "https://console.nace.ai/v1"
    assert nace_route.provider.model == "drex-latest"
    assert nace_route.provider.max_retries == 0
    assert set(nace_route.provider.key_pool._key_map) == {"nace_env_key", "nace_file_key"}
