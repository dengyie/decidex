"""
Unit tests for DecideX KeyPool and TypeSafeJevProvider pool rotation.
"""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, patch
import httpx
import pytest

from decidex.pool import (
    KeyEntry,
    KeyPool,
    KeyState,
    NoAvailableKeysError,
    RotationStrategy,
)
from decidex.providers.typesafe import TypeSafeJevProvider
from decidex.types import ObservationPayload, PrimitiveType, QuestionSpec


def test_key_entry_availability():
    entry = KeyEntry(key="k1", state=KeyState.ACTIVE)
    assert entry.is_available() is True

    # DEAD key is never available
    entry.state = KeyState.DEAD
    assert entry.is_available() is False

    # COOLDOWN key is available only after cooldown_until
    entry.state = KeyState.COOLDOWN
    entry.cooldown_until = time.time() + 100.0
    assert entry.is_available() is False
    assert entry.is_available(now=time.time() + 150.0) is True


@pytest.mark.asyncio
async def test_round_robin_rotation():
    pool = KeyPool.from_keys(["k1", "k2", "k3"], strategy=RotationStrategy.ROUND_ROBIN)
    assert len(pool) == 3

    e1 = await pool.get_next_key()
    e2 = await pool.get_next_key()
    e3 = await pool.get_next_key()
    e4 = await pool.get_next_key()

    assert [e1.key, e2.key, e3.key, e4.key] == ["k1", "k2", "k3", "k1"]
    assert e1.total_requests == 2
    assert e2.total_requests == 1
    assert e3.total_requests == 1


@pytest.mark.asyncio
async def test_least_used_rotation():
    pool = KeyPool.from_keys(["k1", "k2"], strategy=RotationStrategy.LEAST_USED)

    # Force k1 usage
    pool.get_entry("k1").total_requests = 5
    pool.get_entry("k2").total_requests = 1

    next_key = await pool.get_next_key()
    assert next_key.key == "k2"


@pytest.mark.asyncio
async def test_key_quarantine_on_401():
    pool = KeyPool.from_keys(["k1", "k2"], strategy=RotationStrategy.ROUND_ROBIN)

    # Mark k1 dead
    pool.record_revoked("k1", reason="401 Unauthorized")
    assert pool.get_entry("k1").state == KeyState.DEAD

    # Next keys should always be k2
    e1 = await pool.get_next_key()
    e2 = await pool.get_next_key()
    assert e1.key == "k2"
    assert e2.key == "k2"

    # Mark k2 dead -> pool empty
    pool.record_revoked("k2", reason="401 Unauthorized")
    with pytest.raises(NoAvailableKeysError):
        await pool.get_next_key()


@pytest.mark.asyncio
async def test_key_cooldown_on_429():
    pool = KeyPool.from_keys(["k1", "k2"], strategy=RotationStrategy.ROUND_ROBIN, default_cooldown_s=10.0)

    # Trip k1 into cooldown
    pool.record_rate_limit("k1", cooldown_s=5.0)
    assert pool.get_entry("k1").state == KeyState.COOLDOWN

    # k2 is chosen
    e = await pool.get_next_key()
    assert e.key == "k2"

    # Simulate time passing beyond cooldown
    future_time = time.time() + 10.0
    with patch("time.time", return_value=future_time):
        e_revived = await pool.get_next_key()
        # k1 should now be eligible again
        assert pool.get_entry("k1").state == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_concurrent_pool_rotation():
    keys = [f"key_{i}" for i in range(10)]
    pool = KeyPool.from_keys(keys, strategy=RotationStrategy.ROUND_ROBIN)

    async def fetch():
        return await pool.get_next_key()

    # 100 concurrent requests
    results = await asyncio.gather(*(fetch() for _ in range(100)))
    assert len(results) == 100

    stats = pool.stats()
    assert stats["total_requests"] == 100
    # Every key should have been picked exactly 10 times
    for k in keys:
        assert pool.get_entry(k).total_requests == 10


def test_markdown_loader(tmp_path):
    md_content = """# Title
| # | Email | Key | key id | org |
| 1 | `user1@example.com` | `apikey_111111_222222` | `key_1` | `Org 1` |
| 2 | `user2@example.com` | `apikey_333333_444444` | `key_2` | `Org 2` |
"""
    p = tmp_path / "test_keys.md"
    p.write_text(md_content, encoding="utf-8")

    pool = KeyPool.from_markdown(str(p))
    assert len(pool) == 2
    assert pool.get_entry("apikey_111111_222222").email == "user1@example.com"
    assert pool.get_entry("apikey_333333_444444").key_id == "key_2"


def test_save_and_load_state(tmp_path):
    pool = KeyPool.from_keys(["k1", "k2"])
    pool.record_success("k1")
    pool.record_revoked("k2", reason="revoked")

    state_file = tmp_path / "pool_state.json"
    pool.save_state(str(state_file))

    new_pool = KeyPool.from_keys(["k1", "k2"])
    new_pool.load_state(str(state_file))

    assert new_pool.get_entry("k1").success_count == 1
    assert new_pool.get_entry("k2").state == KeyState.DEAD


def test_from_default_locations(tmp_path, monkeypatch):
    test_key_file = tmp_path / "custom_keys.txt"
    test_key_file.write_text("apikey_default_loc_1\napikey_default_loc_2\n", encoding="utf-8")

    monkeypatch.setenv("DECIDEX_KEY_FILE", str(test_key_file))
    pool = KeyPool.from_default_locations()
    assert len(pool) == 2
    assert pool.get_entry("apikey_default_loc_1") is not None


@pytest.mark.asyncio
async def test_typesafe_provider_rotation_and_fallback():
    pool = KeyPool.from_keys(["bad_key", "good_key"], strategy=RotationStrategy.ROUND_ROBIN)
    provider = TypeSafeJevProvider(key_pool=pool, max_retries=2)

    questions = [
        QuestionSpec(
            id="action",
            primitive=PrimitiveType.CHOICE,
            description="Next action",
            options=["LEFT", "RIGHT"]
        )
    ]
    payload = ObservationPayload(observation={"x": 10})

    call_count = 0

    async def mock_post(url, json, headers):
        nonlocal call_count
        call_count += 1
        auth_header = headers.get("Authorization", "")

        if "bad_key" in auth_header:
            req = httpx.Request("POST", url)
            return httpx.Response(401, request=req, json={"error": "Unauthorized"})

        # good_key returns 200 with official System One shape
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={
            "model": "jev-1.13.0",
            "answers": {
                "action": {
                    "type": "choice",
                    "choice": "RIGHT",
                    "probabilities": {"LEFT": 0.1, "RIGHT": 0.9},
                    "confidence": 0.9
                }
            }
        })

    with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
        verdicts = await provider.infer(payload, questions)
        assert len(verdicts) == 1
        assert verdicts[0].selected == "RIGHT"
        assert verdicts[0].raw_confidence == 0.9

        # bad_key should be quarantined as DEAD
        assert pool.get_entry("bad_key").state == KeyState.DEAD
        # good_key should be ACTIVE with 1 success
        assert pool.get_entry("good_key").state == KeyState.ACTIVE
        assert pool.get_entry("good_key").success_count == 1
        assert call_count == 2


@pytest.mark.asyncio
async def test_typesafe_provider_raw_confidence_from_probabilities():
    pool = KeyPool.from_keys(["good_key"])
    provider = TypeSafeJevProvider(key_pool=pool)
    payload = ObservationPayload(domain="test", observation={})
    questions = [
        QuestionSpec(id="q1", primitive=PrimitiveType.CHOICE, description="Test question", options=["A", "B"])
    ]

    async def mock_post(url, json, headers):
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={
            "answers": {
                "q1": {
                    "type": "choice",
                    "choice": "A",
                    # confidence represents margin metric (0.3), but probability of A is 0.65
                    "confidence": 0.3,
                    "probabilities": {"A": 0.65, "B": 0.35}
                }
            }
        })

    with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
        verdicts = await provider.infer(payload, questions)
        assert len(verdicts) == 1
        assert verdicts[0].selected == "A"
        assert verdicts[0].raw_confidence == 0.65


@pytest.mark.asyncio
async def test_typesafe_provider_warmup():
    pool = KeyPool.from_keys(["k1"])
    provider = TypeSafeJevProvider(key_pool=pool)

    async def mock_get(url, timeout=None):
        req = httpx.Request("GET", url)
        return httpx.Response(200, request=req)

    with patch.object(httpx.AsyncClient, "get", side_effect=mock_get):
        success = await provider.warmup()
        assert success is True
    await provider.aclose()


@pytest.mark.asyncio
async def test_typesafe_provider_decisions_endpoint_passthrough():
    """MindsHub-style base_url ending in /decisions must be posted to as-is (no /systemone suffix)."""
    pool = KeyPool.from_keys(["mh_key"])
    provider = TypeSafeJevProvider(
        base_url="https://api.mindshub.ai/v1/decisions",
        model="jev",
        key_pool=pool,
    )
    payload = ObservationPayload(domain="test", observation={})
    questions = [
        QuestionSpec(id="q1", primitive=PrimitiveType.CHOICE, description="Test", options=["A", "B"])
    ]

    captured_urls = []

    async def mock_post(url, json, headers):
        captured_urls.append(url)
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={
            "model": "jev-1.13.0",
            "answers": {
                "q1": {"type": "choice", "choice": "A", "probabilities": {"A": 0.7, "B": 0.3}}
            }
        })

    with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
        verdicts = await provider.infer(payload, questions)

    assert len(verdicts) == 1
    assert captured_urls == ["https://api.mindshub.ai/v1/decisions"]
    await provider.aclose()


def test_provider_base_url_env_override(monkeypatch):
    monkeypatch.setenv("DECIDEX_BASE_URL", "https://api.mindshub.ai/v1/decisions")
    provider = TypeSafeJevProvider()
    assert provider.base_url == "https://api.mindshub.ai/v1/decisions"

    # Explicit constructor argument wins over the environment
    provider = TypeSafeJevProvider(base_url="https://api.typesafe.ai/v1")
    assert provider.base_url == "https://api.typesafe.ai/v1"


def test_from_default_locations_ignores_mindshub_credentials(tmp_path, monkeypatch):
    """from_default_locations is the OFFICIAL pool locator: mindshub-tagged keys
    (env or org=mindshub file entries) must never be returned, even alone."""
    monkeypatch.setenv("MINDSHUB_API_KEY", "mdb_env_key")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DECIDEX_API_KEY", raising=False)
    monkeypatch.setattr(
        KeyPool, "default_key_file_candidates",
        classmethod(lambda cls: [str(tmp_path / "keys.jsonl")]),
    )
    monkeypatch.setattr(KeyPool, "vault_key_note_candidates", classmethod(lambda cls: []))
    (tmp_path / "keys.jsonl").write_text('{"key": "mdb_file_key", "org": "mindshub"}\n', encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        KeyPool.from_default_locations()


def test_from_default_locations_filters_tagged_mindshub_keys(tmp_path, monkeypatch):
    """A mixed keys file contributes only its official (untagged) entries."""
    monkeypatch.delenv("MINDSHUB_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DECIDEX_API_KEY", raising=False)
    monkeypatch.setattr(
        KeyPool, "default_key_file_candidates",
        classmethod(lambda cls: [str(tmp_path / "keys.jsonl")]),
    )
    monkeypatch.setattr(KeyPool, "vault_key_note_candidates", classmethod(lambda cls: []))
    (tmp_path / "keys.jsonl").write_text(
        '{"key": "mdb_file_key", "org": "mindshub"}\n'
        '{"key": "apikey_official_1"}\n',
        encoding="utf-8",
    )

    pool = KeyPool.from_default_locations()
    assert set(pool._key_map) == {"apikey_official_1"}


def test_default_key_file_candidates_prefers_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DECIDEX_KEY_FILE", str(tmp_path / "custom.txt"))
    assert KeyPool.default_key_file_candidates()[0] == str(tmp_path / "custom.txt")

    monkeypatch.delenv("DECIDEX_KEY_FILE", raising=False)
    assert KeyPool.default_key_file_candidates()[0] == "keys.jsonl"


def test_vault_key_note_candidates_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DECIDEX_VAULT", str(tmp_path))
    notes = KeyPool.vault_key_note_candidates()
    assert notes == [str(tmp_path / "Note" / "accounts" / "ai" / "typesafe API keys.md")]


def test_keypool_exponential_backoff_and_cooldown_recovery():
    pool = KeyPool.from_keys(["rate_key"], default_cooldown_s=10.0)
    entry = pool.get_entry("rate_key")
    assert entry.state == KeyState.ACTIVE

    # 1st 429
    t_start = time.time()
    pool.record_rate_limit("rate_key")
    assert entry.state == KeyState.COOLDOWN
    assert 9.0 <= (entry.cooldown_until - t_start) <= 11.5

    # 2nd consecutive 429 -> 2x multiplier
    pool.record_rate_limit("rate_key")
    assert 19.0 <= (entry.cooldown_until - t_start) <= 22.0

    # 3rd consecutive 429 -> 4x multiplier
    pool.record_rate_limit("rate_key")
    assert 39.0 <= (entry.cooldown_until - t_start) <= 42.0

    # Simulate time passing beyond cooldown
    pool._refresh_cooldowns(now=t_start + 100.0)
    assert entry.state == KeyState.ACTIVE
    assert entry.consecutive_failures == 0


@pytest.mark.asyncio
async def test_keypool_acquire_retry_after_header():
    pool = KeyPool.from_keys(["rate_key"])
    entry = pool.get_entry("rate_key")

    req = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone")
    resp_429 = httpx.Response(429, headers={"Retry-After": "75"}, request=req)
    err = httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp_429)

    now = time.time()
    with pytest.raises(httpx.HTTPStatusError):
        async with pool.acquire():
            raise err

    assert entry.state == KeyState.COOLDOWN
    assert 73.0 <= (entry.cooldown_until - now) <= 77.0


def test_keypool_atomic_save_and_corrupted_load(tmp_path):
    state_file = str(tmp_path / "pool_state.json")
    pool = KeyPool.from_keys(["key_1", "key_2"])
    pool.record_rate_limit("key_1", cooldown_s=300.0)
    pool.record_revoked("key_2", reason="Token expired")

    # 1. Atomic save
    pool.save_state(state_file)
    assert os.path.exists(state_file)

    # 2. Fresh pool loads state correctly
    fresh_pool = KeyPool.from_keys(["key_1", "key_2"])
    fresh_pool.load_state(state_file)
    assert fresh_pool.get_entry("key_1").state == KeyState.COOLDOWN
    assert fresh_pool.get_entry("key_2").state == KeyState.DEAD

    # 3. Corrupt file does not crash load_state
    with open(state_file, "w", encoding="utf-8") as f:
        f.write("{invalid_json_content: corrupt")

    pool_survivor = KeyPool.from_keys(["key_1"])
    pool_survivor.load_state(state_file)
    assert pool_survivor.get_entry("key_1").state == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_typesafe_provider_safe_float_none_and_zero_confidence():
    pool = KeyPool.from_keys(["test_key"])
    provider = TypeSafeJevProvider(key_pool=pool)
    payload = ObservationPayload(domain="test", observation={})
    questions = [
        QuestionSpec(id="q_none", primitive=PrimitiveType.CHOICE, description="None conf", options=["A", "B"]),
        QuestionSpec(id="q_zero", primitive=PrimitiveType.CHOICE, description="Zero conf", options=["X", "Y"])
    ]

    async def mock_post(url, json, headers):
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={
            "answers": {
                "q_none": {"choice": "A", "confidence": None},
                "q_zero": {"choice": "X", "confidence": 0.0}
            }
        })

    with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
        verdicts = await provider.infer(payload, questions)

    assert len(verdicts) == 2
    v_none = next(v for v in verdicts if v.id == "q_none")
    v_zero = next(v for v in verdicts if v.id == "q_zero")
    assert v_none.selected == "A"
    assert v_none.raw_confidence == 1.0  # Safe default on None
    assert v_zero.selected == "X"
    assert v_zero.raw_confidence == 0.0  # Preserves actual 0.0 float without converting to 1.0


def test_typesafe_provider_score_dictionary_criteria():
    provider = TypeSafeJevProvider(api_key="dummy_key")
    payload = ObservationPayload(domain="game", observation={"step": 1})
    q_score = QuestionSpec(
        id="score_head",
        primitive=PrimitiveType.SCORE,
        description="Rate game state",
        scale=(1.0, 5.0),
        criteria={"1": "Critical state", "5": "Excellent state"}
    )
    body = provider._format_request_body(payload, [q_score])
    q_out = body["questions"]["score_head"]
    assert q_out["type"] == "score"
    assert q_out["criteria"] == {"1": "Critical state", "5": "Excellent state"}



@pytest.mark.asyncio
async def test_typesafe_provider_retry_after_header():
    pool = KeyPool.from_keys(["k429"])
    provider = TypeSafeJevProvider(key_pool=pool, max_retries=1)
    payload = ObservationPayload(domain="test", observation={})
    questions = [
        QuestionSpec(id="q1", primitive=PrimitiveType.CHOICE, description="Test", options=["A", "B"])
    ]

    async def mock_post(url, json, headers):
        req = httpx.Request("POST", url)
        # Return 429 with Retry-After: 45
        return httpx.Response(429, request=req, headers={"Retry-After": "45"})

    with patch.object(httpx.AsyncClient, "post", side_effect=mock_post):
        with pytest.raises(RuntimeError):
            await provider.infer(payload, questions)

    entry = pool.get_entry("k429")
    assert entry.state == KeyState.COOLDOWN
    # Cooldown duration should be approx 45s from Retry-After header
    remaining = entry.cooldown_until - time.time()
    assert 43.0 <= remaining <= 46.0


def test_keypool_atomic_save_and_corrupted_load(tmp_path):
    state_file = str(tmp_path / "pool_state.json")
    pool = KeyPool.from_keys(["key_a", "key_b"])
    pool.record_revoked("key_a", reason="Revoked in test")
    pool.save_state(state_file)

    # Verify file was written
    assert os.path.exists(state_file)
    with open(state_file, "r") as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert any(item["key"] == "key_a" and item["state"] == "dead" for item in data)

    # Verify load_state updates pool
    pool_to_load = KeyPool.from_keys(["key_a", "key_b"])
    assert pool_to_load.get_entry("key_a").state == KeyState.ACTIVE
    pool_to_load.load_state(state_file)
    assert pool_to_load.get_entry("key_a").state == KeyState.DEAD

    # Test corrupted json handling in load_state
    with open(state_file, "w") as f:
        f.write("{invalid_json: truncated...")

    pool_corrupt = KeyPool.from_keys(["key_a", "key_b"])
    pool_corrupt.load_state(state_file)
    assert pool_corrupt.get_entry("key_a").state == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_keypool_thread_safe_concurrent_ops():
    """Tests high-concurrency async and threaded access to KeyPool mutations and rotations."""
    keys = [f"key_{i}" for i in range(20)]
    pool = KeyPool.from_keys(keys, strategy=RotationStrategy.ROUND_ROBIN)

    async def worker(idx: int):
        for _ in range(15):
            try:
                entry = await pool.get_next_key()
            except NoAvailableKeysError:
                await asyncio.sleep(0.03)
                continue

            if idx % 3 == 0:
                pool.record_success(entry.key, latency_ms=10.0)
            elif idx % 3 == 1:
                pool.record_rate_limit(entry.key, cooldown_s=0.02)
            else:
                pool.record_failure(entry.key, error="transient error")
            await asyncio.sleep(0.001)

    tasks = [asyncio.create_task(worker(i)) for i in range(10)]
    await asyncio.gather(*tasks)

    st = pool.stats()
    assert st["total_keys"] == 20
    assert st["total_requests"] > 0


@pytest.mark.asyncio
async def test_acquire_precise_status_code():
    """Verifies pool.acquire() distinguishes status_code == 401 without fuzzy substring misattribution."""
    pool = KeyPool.from_keys(["precise_key"])

    class CustomErrorWithNumber401(Exception):
        pass

    # An exception whose message contains "401" in text (e.g. score was 401) but status code is not 401
    with pytest.raises(CustomErrorWithNumber401):
        async with pool.acquire():
            raise CustomErrorWithNumber401("Calculated score 401 on move 5")

    entry = pool.get_entry("precise_key")
    # Must NOT be marked DEAD because status code was not 401!
    assert entry.state != KeyState.DEAD
    assert entry.failure_count == 1


@pytest.mark.asyncio
async def test_keypool_o1_fast_path_and_exhaustion():
    """Verifies that O(1) amortized round-robin probes keys in cyclic order and raises informative error when exhausted."""
    keys = [f"k_{i}" for i in range(5)]
    pool = KeyPool.from_keys(keys, strategy=RotationStrategy.ROUND_ROBIN, default_cooldown_s=100.0)

    # 1. Sequential retrieval cycles through all keys in order
    retrieved = []
    for _ in range(5):
        entry = await pool.get_next_key()
        retrieved.append(entry.key)
    assert retrieved == keys

    # Next call wraps around to k_0
    wrap_entry = await pool.get_next_key()
    assert wrap_entry.key == "k_0"

    # 2. Put all keys in cooldown
    for k in keys:
        pool.record_rate_limit(k)

    # 3. get_next_key must raise NoAvailableKeysError with cooling down detail
    with pytest.raises(NoAvailableKeysError) as exc_info:
        await pool.get_next_key()
    assert "cooling down" in str(exc_info.value)
    assert "next ready in" in str(exc_info.value)

    # 4. Mark all dead
    for k in keys:
        pool.record_revoked(k, reason="401 unauthorized")

    with pytest.raises(NoAvailableKeysError) as exc_info_dead:
        await pool.get_next_key()
    assert "DEAD" in str(exc_info_dead.value)


def test_from_default_locations_env_var(monkeypatch):
    """Verifies that KeyPool.from_default_locations() discovers TYPESAFE_API_KEY when no files exist."""
    # Ensure no file candidates exist in cwd
    monkeypatch.delenv("DECIDEX_KEY_FILE", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey_env_123_456")
    pool = KeyPool.from_default_locations()
    assert len(pool) == 1
    assert pool.get_entry("apikey_env_123_456") is not None


@pytest.mark.asyncio
async def test_least_used_rotation_single_pass():
    """Verifies that LEAST_USED rotation selects key with fewest total requests and handles cooldowns."""
    pool = KeyPool.from_keys(["k_busy", "k_free"], strategy=RotationStrategy.LEAST_USED)
    # k_busy has 5 requests, k_free has 0
    pool.get_entry("k_busy").total_requests = 5
    pool.get_entry("k_free").total_requests = 0

    entry = await pool.get_next_key()
    assert entry.key == "k_free"

    # Put k_free in cooldown
    pool.record_rate_limit("k_free")
    entry2 = await pool.get_next_key()
    assert entry2.key == "k_busy"


def test_keypool_save_state_temp_cleanup_on_exception(tmp_path):
    """Verifies that save_state cleans up orphan temporary file if os.replace fails."""
    pool = KeyPool.from_keys(["k1", "k2"])
    target_path = str(tmp_path / "subdir" / "state.json")

    with patch("os.replace", side_effect=OSError("Disk full or permission denied")):
        with pytest.raises(OSError):
            pool.save_state(target_path)

    # Confirm no temp files remain in target directory
    sub_dir = tmp_path / "subdir"
    assert sub_dir.exists()
    assert list(sub_dir.glob("tmp*")) == []
    assert not os.path.exists(target_path)


def test_keypool_record_rate_limit_clamps_cooldown():
    """Verifies that record_rate_limit clamps excessive Retry-After to 3600s and lower bound to 1.0s."""
    pool = KeyPool.from_keys(["k_clamp"])
    entry = pool.get_entry("k_clamp")
    now = time.time()

    # Huge cooldown 1,000,000s should be clamped to 3600s
    pool.record_rate_limit("k_clamp", cooldown_s=1_000_000.0)
    assert 3590.0 <= (entry.cooldown_until - now) <= 3605.0

    # Negative or tiny cooldown should be clamped to at least 1.0s
    pool.record_rate_limit("k_clamp", cooldown_s=-50.0)
    # Consecutive failures is now 2, so backoff kicks in when cooldown_s <= 0
    # But if positive tiny e.g. 0.2s is passed:
    pool.record_rate_limit("k_clamp", cooldown_s=0.2)
    assert 0.9 <= (entry.cooldown_until - time.time()) <= 1.5





