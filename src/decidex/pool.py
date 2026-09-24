"""
KeyPool implementation for high-concurrency Jev / TypeSafe account pool management.
Supports Round-Robin, Least-Used, and Random rotation strategies,
with automatic 429 cooldowns, 401 permanent dead key isolation, and state telemetry.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Union


class KeyState(str, Enum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    DEAD = "dead"


class RotationStrategy(str, Enum):
    ROUND_ROBIN = "round_robin"
    LEAST_USED = "least_used"
    RANDOM = "random"


@dataclass
class KeyEntry:
    """Represents a single API key and its runtime operational telemetry."""
    key: str
    state: KeyState = KeyState.ACTIVE
    key_id: Optional[str] = None
    email: Optional[str] = None
    org: Optional[str] = None
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_used_at: Optional[float] = None
    last_error: Optional[str] = None
    cooldown_until: float = 0.0

    def is_available(self, now: Optional[float] = None) -> bool:
        """Checks if key is usable right now."""
        if self.state == KeyState.DEAD:
            return False
        current = time.time() if now is None else now
        if self.state == KeyState.COOLDOWN:
            return current >= self.cooldown_until
        return self.state == KeyState.ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> KeyEntry:
        state_val = data.get("state", "active")
        return cls(
            key=data["key"],
            state=KeyState(state_val) if isinstance(state_val, str) else state_val,
            key_id=data.get("key_id"),
            email=data.get("email"),
            org=data.get("org"),
            total_requests=data.get("total_requests", 0),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            consecutive_failures=data.get("consecutive_failures", 0),
            last_used_at=data.get("last_used_at"),
            last_error=data.get("last_error"),
            cooldown_until=data.get("cooldown_until", 0.0)
        )


class NoAvailableKeysError(RuntimeError):
    """Raised when all keys in the pool are dead or currently in cooldown."""
    pass


class KeyPool:
    """
    Thread-safe and async-safe KeyPool for rotating across large fleets of Jev / TypeSafe accounts.
    """

    def __init__(
        self,
        entries: Optional[List[KeyEntry]] = None,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        default_cooldown_s: float = 60.0,
        max_consecutive_failures: int = 3
    ):
        self.strategy = strategy
        self.default_cooldown_s = default_cooldown_s
        self.max_consecutive_failures = max_consecutive_failures
        self._entries: List[KeyEntry] = []
        self._key_map: Dict[str, KeyEntry] = {}
        self._index: int = 0
        self._sync_lock = threading.RLock()
        self._async_lock: Optional[asyncio.Lock] = None

        if entries:
            for entry in entries:
                self.add_entry(entry)

    @property
    def async_lock(self) -> asyncio.Lock:
        """Lazily initialize asyncio.Lock to bind to active event loop."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def __len__(self) -> int:
        with self._sync_lock:
            return len(self._entries)

    def add_entry(self, entry: KeyEntry) -> None:
        with self._sync_lock:
            if entry.key in self._key_map:
                return
            self._entries.append(entry)
            self._key_map[entry.key] = entry

    def add_key(
        self,
        key: str,
        email: Optional[str] = None,
        key_id: Optional[str] = None,
        org: Optional[str] = None
    ) -> KeyEntry:
        with self._sync_lock:
            if key in self._key_map:
                return self._key_map[key]
            entry = KeyEntry(key=key, email=email, key_id=key_id, org=org)
            self.add_entry(entry)
            return entry

    def get_entry(self, key: str) -> Optional[KeyEntry]:
        with self._sync_lock:
            return self._key_map.get(key)

    def _refresh_cooldowns(self, now: float) -> None:
        """Transitions expired cooldown keys back to active status (caller must hold _sync_lock)."""
        for entry in self._entries:
            if entry.state == KeyState.COOLDOWN and now >= entry.cooldown_until:
                entry.state = KeyState.ACTIVE
                entry.consecutive_failures = 0

    async def get_next_key(self) -> KeyEntry:
        """
        Retrieves the next eligible key according to the configured rotation strategy.
        Raises NoAvailableKeysError if no active or cooldown-cleared keys are found.
        """
        async with self.async_lock:
            with self._sync_lock:
                if not self._entries:
                    raise NoAvailableKeysError("KeyPool is empty; no keys registered.")

                now = time.time()
                self._refresh_cooldowns(now)

                selected_entry: Optional[KeyEntry] = None

                if self.strategy == RotationStrategy.ROUND_ROBIN:
                    # O(1) amortized fast-path: check from current index without scanning full pool
                    total = len(self._entries)
                    for _ in range(total):
                        candidate = self._entries[self._index]
                        self._index = (self._index + 1) % total
                        if candidate.is_available(now):
                            selected_entry = candidate
                            break
                    if selected_entry is None:
                        cooldowns = [e.cooldown_until for e in self._entries if e.state == KeyState.COOLDOWN]
                        if cooldowns:
                            min_wait = max(0.0, min(cooldowns) - now)
                            raise NoAvailableKeysError(
                                f"All {len(self._entries)} keys are unavailable. "
                                f"{len(cooldowns)} keys cooling down (next ready in {min_wait:.1f}s)."
                            )
                        raise NoAvailableKeysError(
                            f"All {len(self._entries)} keys are marked DEAD. No keys available."
                        )
                elif self.strategy == RotationStrategy.LEAST_USED:
                    # Single-pass selection prioritizing keys with fewest total requests
                    selected_entry = min(
                        (e for e in self._entries if e.is_available(now)),
                        key=lambda e: (e.total_requests, e.last_used_at or 0.0),
                        default=None
                    )
                    if selected_entry is None:
                        cooldowns = [e.cooldown_until for e in self._entries if e.state == KeyState.COOLDOWN]
                        if cooldowns:
                            min_wait = max(0.0, min(cooldowns) - now)
                            raise NoAvailableKeysError(
                                f"All {len(self._entries)} keys are unavailable. "
                                f"{len(cooldowns)} keys cooling down (next ready in {min_wait:.1f}s)."
                            )
                        raise NoAvailableKeysError(
                            f"All {len(self._entries)} keys are marked DEAD. No keys available."
                        )
                else:
                    available = [e for e in self._entries if e.is_available(now)]
                    if not available:
                        cooldowns = [e.cooldown_until for e in self._entries if e.state == KeyState.COOLDOWN]
                        if cooldowns:
                            min_wait = max(0.0, min(cooldowns) - now)
                            raise NoAvailableKeysError(
                                f"All {len(self._entries)} keys are unavailable. "
                                f"{len(cooldowns)} keys cooling down (next ready in {min_wait:.1f}s)."
                            )
                        raise NoAvailableKeysError(
                            f"All {len(self._entries)} keys are marked DEAD. No keys available."
                        )

                    if self.strategy == RotationStrategy.RANDOM:
                        selected_entry = random.choice(available)
                    else:
                        selected_entry = available[0]

                assert selected_entry is not None

                selected_entry.total_requests += 1
                selected_entry.last_used_at = now
                return selected_entry

    def record_success(self, key: str, latency_ms: float = 0.0) -> None:
        """Marks a successful request execution for the key."""
        with self._sync_lock:
            entry = self._key_map.get(key)
            if not entry:
                return
            entry.success_count += 1
            entry.consecutive_failures = 0
            entry.state = KeyState.ACTIVE

    def record_rate_limit(
        self,
        key: str,
        cooldown_s: Optional[float] = None,
        reason: str = "429 Too Many Requests"
    ) -> None:
        """Places a key into temporary cooldown with exponential backoff escalation."""
        with self._sync_lock:
            entry = self._key_map.get(key)
            if not entry:
                return
            entry.failure_count += 1
            entry.consecutive_failures += 1
            entry.last_used_at = time.time()
            entry.state = KeyState.COOLDOWN

            if cooldown_s is not None and cooldown_s > 0:
                duration = cooldown_s
            else:
                # Exponential backoff capped at 600s
                multiplier = min(10, 2 ** min(5, entry.consecutive_failures - 1))
                duration = min(600.0, self.default_cooldown_s * multiplier)

            entry.cooldown_until = time.time() + duration
            entry.last_error = f"COOLDOWN ({duration:.1f}s): {reason}"

    def record_revoked(self, key: str, reason: str = "401 Unauthorized") -> None:
        """Permanently marks a key as DEAD so it will not be selected again."""
        with self._sync_lock:
            entry = self._key_map.get(key)
            if not entry:
                return
            entry.state = KeyState.DEAD
            entry.failure_count += 1
            entry.last_error = f"DEAD: {reason}"

    def record_failure(self, key: str, error: str, is_transient: bool = True) -> None:
        """Records a general network/server failure; cools down after consecutive limits."""
        with self._sync_lock:
            entry = self._key_map.get(key)
            if not entry:
                return
            entry.failure_count += 1
            entry.consecutive_failures += 1
            entry.last_error = error

            if entry.consecutive_failures >= self.max_consecutive_failures:
                # Trip into cooldown
                entry.state = KeyState.COOLDOWN
                entry.cooldown_until = time.time() + self.default_cooldown_s

    def revive_all(self) -> int:
        """Resets all keys (including DEAD and COOLDOWN) back to ACTIVE."""
        with self._sync_lock:
            count = 0
            for entry in self._entries:
                if entry.state != KeyState.ACTIVE:
                    entry.state = KeyState.ACTIVE
                    entry.consecutive_failures = 0
                    entry.cooldown_until = 0.0
                    count += 1
            return count

    def revive_key(self, key: str) -> bool:
        with self._sync_lock:
            entry = self._key_map.get(key)
            if entry:
                entry.state = KeyState.ACTIVE
                entry.consecutive_failures = 0
                entry.cooldown_until = 0.0
                return True
            return False

    def stats(self) -> Dict[str, Any]:
        """Returns aggregate operational statistics of the pool."""
        with self._sync_lock:
            now = time.time()
            self._refresh_cooldowns(now)

            active = sum(1 for e in self._entries if e.state == KeyState.ACTIVE)
            cooldown = sum(1 for e in self._entries if e.state == KeyState.COOLDOWN)
            dead = sum(1 for e in self._entries if e.state == KeyState.DEAD)
            total_reqs = sum(e.total_requests for e in self._entries)
            total_success = sum(e.success_count for e in self._entries)
            total_fail = sum(e.failure_count for e in self._entries)

            success_rate = (total_success / total_reqs) if total_reqs > 0 else 1.0

            return {
                "total_keys": len(self._entries),
                "active_keys": active,
                "cooldown_keys": cooldown,
                "dead_keys": dead,
                "total_requests": total_reqs,
                "total_success": total_success,
                "total_failures": total_fail,
                "success_rate": round(success_rate, 4),
                "strategy": self.strategy.value
            }

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[KeyEntry]:
        """
        Context manager for acquiring a key and automatically classifying its execution result.
        Usage:
            async with pool.acquire() as entry:
                resp = await client.post(..., headers={"Authorization": f"Bearer {entry.key}"})
        """
        entry = await self.get_next_key()
        start = time.perf_counter()
        try:
            yield entry
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self.record_success(entry.key, elapsed_ms)
        except Exception as exc:
            err_msg = str(exc)
            # Only quarantine on explicit HTTP 401 Unauthorized status
            status_code = None
            if hasattr(exc, "response"):
                status_code = getattr(exc.response, "status_code", None)
            elif hasattr(exc, "status_code"):
                status_code = getattr(exc, "status_code", None)

            is_401 = (status_code == 401) or ("status code: 401" in err_msg.lower()) or ("401 unauthorized" in err_msg.lower())
            is_429 = (status_code == 429) or ("status code: 429" in err_msg.lower()) or ("429 too many requests" in err_msg.lower())

            if is_401:
                self.record_revoked(entry.key, reason=err_msg)
            elif is_429:
                self.record_rate_limit(entry.key, reason=err_msg)
            else:
                self.record_failure(entry.key, error=err_msg, is_transient=True)
            raise

    # -------------------------------------------------------------------------
    # Builders & Loaders
    # -------------------------------------------------------------------------

    @classmethod
    def from_keys(
        cls,
        keys: List[str],
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        default_cooldown_s: float = 60.0
    ) -> KeyPool:
        entries = [KeyEntry(key=k.strip()) for k in keys if k and k.strip()]
        return cls(entries=entries, strategy=strategy, default_cooldown_s=default_cooldown_s)

    @classmethod
    def from_file(
        cls,
        file_path: str,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        default_cooldown_s: float = 60.0
    ) -> KeyPool:
        """
        Loads keys from a plain text (one key per line), json, or jsonl file.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Key file not found: {file_path}")

        entries: List[KeyEntry] = []
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if file_path.endswith(".json") and (content.startswith("[") or content.startswith("{")):
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        entries.append(KeyEntry(key=item.strip()))
                    elif isinstance(item, dict) and "key" in item:
                        entries.append(KeyEntry.from_dict(item))
            elif isinstance(data, dict) and "keys" in data:
                for k in data["keys"]:
                    if isinstance(k, str):
                        entries.append(KeyEntry(key=k.strip()))
                    elif isinstance(k, dict) and "key" in k:
                        entries.append(KeyEntry.from_dict(k))
        else:
            # Plain text / JSONL fallback
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("{") and line.endswith("}"):
                    try:
                        d = json.loads(line)
                        if "key" in d:
                            entries.append(KeyEntry.from_dict(d))
                            continue
                    except json.JSONDecodeError:
                        pass
                entries.append(KeyEntry(key=line))

        return cls(entries=entries, strategy=strategy, default_cooldown_s=default_cooldown_s)

    @classmethod
    def from_markdown(
        cls,
        markdown_path: str,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        default_cooldown_s: float = 60.0
    ) -> KeyPool:
        """
        Parses keys directly from the Obsidian note table format:
        | # | Email | Key | key id | org |
        """
        if not os.path.exists(markdown_path):
            raise FileNotFoundError(f"Markdown note not found: {markdown_path}")

        entries: List[KeyEntry] = []
        key_pattern = re.compile(
            r'\|\s*\d+\s*\|\s*`([^`]+)`\s*\|\s*`(apikey_[a-f0-9]+_[a-f0-9]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|'
        )
        fallback_pattern = re.compile(r'`(apikey_[a-f0-9]+_[a-f0-9]+)`')

        with open(markdown_path, "r", encoding="utf-8") as f:
            for line in f:
                line_str = line.strip()
                m = key_pattern.search(line_str)
                if m:
                    email, key, key_id, org = m.groups()
                    entries.append(KeyEntry(
                        key=key,
                        email=email,
                        key_id=key_id,
                        org=org
                    ))
                else:
                    m2 = fallback_pattern.search(line_str)
                    if m2:
                        entries.append(KeyEntry(key=m2.group(1)))

        return cls(entries=entries, strategy=strategy, default_cooldown_s=default_cooldown_s)

    @classmethod
    def from_obsidian_vault(
        cls,
        vault_path: Optional[str] = None,
        note_rel_path: str = "Note/accounts/ai/typesafe API keys.md",
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        default_cooldown_s: float = 60.0
    ) -> KeyPool:
        """
        Convenience builder resolving the standard iCloud Obsidian note location.
        """
        if vault_path is None:
            vault_path = os.path.expanduser(
                "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/obsidian-note"
            )
        full_path = os.path.join(vault_path, note_rel_path)
        return cls.from_markdown(full_path, strategy=strategy, default_cooldown_s=default_cooldown_s)

    @classmethod
    def from_default_locations(
        cls,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        default_cooldown_s: float = 60.0
    ) -> KeyPool:
        """
        Attempts to locate and load the key pool from standard runtime candidates:
        1. DECIDEX_KEY_FILE environment variable
        2. Local working directory './keys.jsonl', './keys.json', './keys.txt'
        3. Standard Linux / server path '/data/decidex/keys.jsonl'
        4. Standard macOS Obsidian vault path
        """
        # 1. Key file path via DECIDEX_KEY_FILE environment variable
        env_file = os.environ.get("DECIDEX_KEY_FILE")
        if env_file and os.path.exists(env_file):
            return cls.from_file(env_file, strategy=strategy, default_cooldown_s=default_cooldown_s)

        # 2. Direct API key environment variable (Docker, CI/CD, Serverless, single or comma-separated)
        env_key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("DECIDEX_API_KEY")
        if env_key:
            keys = [k.strip() for k in env_key.split(",") if k.strip()]
            if keys:
                return cls.from_keys(keys, strategy=strategy, default_cooldown_s=default_cooldown_s)

        # 3. Local working directory or server candidates
        for candidate in ("keys.jsonl", "keys.json", "keys.txt", "/data/decidex/keys.jsonl", "/data/decidex/keys.txt"):
            if os.path.exists(candidate):
                return cls.from_file(candidate, strategy=strategy, default_cooldown_s=default_cooldown_s)

        # 4. Standard macOS Obsidian vault path
        obsidian_path = os.path.expanduser(
            "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/obsidian-note/Note/accounts/ai/typesafe API keys.md"
        )
        if os.path.exists(obsidian_path):
            return cls.from_markdown(obsidian_path, strategy=strategy, default_cooldown_s=default_cooldown_s)

        raise FileNotFoundError(
            "Could not automatically locate Jev key pool. Please provide an explicit path via "
            "KeyPool.from_file(path), set the DECIDEX_KEY_FILE env var, or run 'python -m decidex.tools.pool_manager export --out keys.jsonl'."
        )

    def save_state(self, path: str) -> None:
        """Saves current pool telemetry and states to disk via atomic replace and fsync."""
        with self._sync_lock:
            data = [e.to_dict() for e in self._entries]
        target_dir = os.path.dirname(os.path.abspath(path))
        os.makedirs(target_dir, exist_ok=True)
        # Atomic write pattern: write to temporary file in same filesystem, fsync, then atomic rename
        with tempfile.NamedTemporaryFile("w", dir=target_dir, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
            temp_path = tf.name
        os.replace(temp_path, path)

    def load_state(self, path: str) -> None:
        """Loads state back into existing keys in the pool with corruption recovery."""
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            return

        if not isinstance(data, list):
            return

        with self._sync_lock:
            now = time.time()
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                if key and key in self._key_map:
                    existing = self._key_map[key]
                    raw_state = item.get("state", "active")
                    try:
                        existing.state = KeyState(raw_state)
                    except ValueError:
                        existing.state = KeyState.ACTIVE

                    existing.total_requests = int(item.get("total_requests", 0))
                    existing.success_count = int(item.get("success_count", 0))
                    existing.failure_count = int(item.get("failure_count", 0))
                    existing.consecutive_failures = int(item.get("consecutive_failures", 0))
                    existing.last_used_at = item.get("last_used_at")
                    existing.last_error = item.get("last_error")
                    existing.cooldown_until = float(item.get("cooldown_until", 0.0))

                    # If cooldown already expired, recover to ACTIVE
                    if existing.state == KeyState.COOLDOWN and now >= existing.cooldown_until:
                        existing.state = KeyState.ACTIVE
