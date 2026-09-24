"""
CLI tool for Jev / TypeSafe KeyPool management, health probing, and format export.
Usage:
    python -m decidex.tools.pool_manager stats
    python -m decidex.tools.pool_manager export --out keys.jsonl
    python -m decidex.tools.pool_manager probe --sample 5 --timeout 3.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional
import httpx

from decidex.pool import KeyEntry, KeyPool, KeyState, RotationStrategy


async def probe_single_key(
    client: httpx.AsyncClient,
    entry: KeyEntry,
    base_url: str = "https://api.typesafe.ai/v1",
    model: str = "jev-latest"
) -> Dict[str, Any]:
    """
    Sends a minimal 'noul' request to verify key validity against official System One.
    """
    url = f"{base_url.rstrip('/')}/systemone"
    headers = {
        "Authorization": f"Bearer {entry.key}",
        "Content-Type": "application/json",
        "User-Agent": "DecideX-PoolManager/0.1.0"
    }
    payload = {
        "model": model,
        "state": "Health probe verification.",
        "questions": {
            "probe": {
                "type": "noul",
                "instructions": "Is this a valid probe?"
            }
        }
    }

    start = time.perf_counter()
    try:
        resp = await client.post(url, json=payload, headers=headers)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        if resp.status_code == 200:
            data = resp.json()
            returned_model = data.get("model", "unknown")
            return {
                "key_prefix": entry.key[:20] + "...",
                "email": entry.email,
                "status": "HEALTHY",
                "code": 200,
                "model": returned_model,
                "latency_ms": round(elapsed_ms, 2)
            }
        elif resp.status_code == 401:
            return {
                "key_prefix": entry.key[:20] + "...",
                "email": entry.email,
                "status": "DEAD",
                "code": 401,
                "error": "Unauthorized / Revoked",
                "latency_ms": round(elapsed_ms, 2)
            }
        elif resp.status_code == 429:
            return {
                "key_prefix": entry.key[:20] + "...",
                "email": entry.email,
                "status": "RATE_LIMITED",
                "code": 429,
                "error": "Rate limited (cooling down)",
                "latency_ms": round(elapsed_ms, 2)
            }
        else:
            return {
                "key_prefix": entry.key[:20] + "...",
                "email": entry.email,
                "status": "ERROR",
                "code": resp.status_code,
                "error": resp.text[:100],
                "latency_ms": round(elapsed_ms, 2)
            }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return {
            "key_prefix": entry.key[:20] + "...",
            "email": entry.email,
            "status": "NETWORK_FAIL",
            "code": 0,
            "error": str(exc),
            "latency_ms": round(elapsed_ms, 2)
        }


async def probe_pool(
    pool: KeyPool,
    sample_size: Optional[int] = None,
    concurrency: int = 5,
    timeout_s: float = 3.0,
    base_url: str = "https://api.typesafe.ai/v1"
) -> List[Dict[str, Any]]:
    entries = pool._entries
    if sample_size is not None and sample_size < len(entries):
        entries = entries[:sample_size]

    print(f"🔍 Probing {len(entries)} keys with concurrency={concurrency}, timeout={timeout_s}s...")

    sem = asyncio.Semaphore(concurrency)
    results: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        async def _worker(entry: KeyEntry):
            async with sem:
                res = await probe_single_key(client, entry, base_url=base_url)
                results.append(res)
                print(f"  [{res['status']}] {res['key_prefix']} (code={res['code']}) in {res['latency_ms']}ms")

        tasks = [_worker(entry) for entry in entries]
        await asyncio.gather(*tasks)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="DecideX Jev KeyPool Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: stats
    stats_p = subparsers.add_parser("stats", help="Display summary statistics of the KeyPool")
    stats_p.add_argument("--source", type=str, default="auto", choices=["auto", "obsidian", "file"],
                         help="Source of keys (default: auto)")
    stats_p.add_argument("--path", type=str, default=None, help="File path if source=file")

    # Subcommand: export
    export_p = subparsers.add_parser("export", help="Export keys to a local file")
    export_p.add_argument("--source", type=str, default="auto", choices=["auto", "obsidian", "file"],
                          help="Source of keys (default: auto)")
    export_p.add_argument("--path", type=str, default=None, help="File path if source=file")
    export_p.add_argument("--out", type=str, required=True, help="Destination file path (.jsonl, .json, or .txt)")
    export_p.add_argument("--format", type=str, default="jsonl", choices=["jsonl", "json", "txt"])

    # Subcommand: probe
    probe_p = subparsers.add_parser("probe", help="Live probe a subset or all keys")
    probe_p.add_argument("--source", type=str, default="auto", choices=["auto", "obsidian", "file"],
                         help="Source of keys (default: auto)")
    probe_p.add_argument("--path", type=str, default=None, help="File path if source=file")
    probe_p.add_argument("--sample", type=int, default=5, help="Number of keys to sample probe (default: 5)")
    probe_p.add_argument("--workers", type=int, default=5, help="Concurrent workers")
    probe_p.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout seconds")
    probe_p.add_argument("--base-url", type=str, default="https://api.typesafe.ai/v1")

    args = parser.parse_args()

    # Load pool
    source = getattr(args, "source", "auto")
    if source == "file":
        if not args.path:
            print("Error: --path is required when --source=file", file=sys.stderr)
            sys.exit(1)
        pool = KeyPool.from_file(args.path)
    elif source == "obsidian":
        pool = KeyPool.from_obsidian_vault(vault_path=args.path) if args.path else KeyPool.from_obsidian_vault()
    else:  # auto
        if args.path:
            pool = KeyPool.from_file(args.path)
        else:
            pool = KeyPool.from_default_locations()

    if args.command == "stats":
        s = pool.stats()
        print("\n" + "=" * 50)
        print("🔑 DecideX Jev KeyPool Statistics")
        print("=" * 50)
        print(f"Total Registered Keys : {s['total_keys']}")
        print(f"Active Available Keys : {s['active_keys']}")
        print(f"Cooling Down Keys     : {s['cooldown_keys']}")
        print(f"Dead / Revoked Keys   : {s['dead_keys']}")
        print(f"Rotation Strategy     : {s['strategy']}")

        # Count emails by domain
        domains: Dict[str, int] = {}
        for e in pool._entries:
            if e.email and "@" in e.email:
                dom = e.email.split("@")[1]
                domains[dom] = domains.get(dom, 0) + 1
        print(f"Distinct Email Domains: {len(domains)} top domains: {dict(sorted(domains.items(), key=lambda x: -x[1])[:5])}")
        print("=" * 50 + "\n")

    elif args.command == "export":
        out_path = args.out
        fmt = getattr(args, "format", "jsonl")
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

        if fmt == "txt":
            with open(out_path, "w", encoding="utf-8") as f:
                for e in pool._entries:
                    f.write(f"{e.key}\n")
        elif fmt == "json":
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in pool._entries], f, indent=2, ensure_ascii=False)
        elif fmt == "jsonl":
            with open(out_path, "w", encoding="utf-8") as f:
                for e in pool._entries:
                    f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")
        else:
            if out_path.endswith(".txt"):
                with open(out_path, "w", encoding="utf-8") as f:
                    for e in pool._entries:
                        f.write(f"{e.key}\n")
            elif out_path.endswith(".json"):
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump([e.to_dict() for e in pool._entries], f, indent=2, ensure_ascii=False)
            else:
                with open(out_path, "w", encoding="utf-8") as f:
                    for e in pool._entries:
                        f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

        print(f"✅ Successfully exported {len(pool)} keys to {out_path} ({fmt})")

    elif args.command == "probe":
        asyncio.run(probe_pool(
            pool=pool,
            sample_size=args.sample,
            concurrency=args.workers,
            timeout_s=args.timeout,
            base_url=args.base_url
        ))


if __name__ == "__main__":
    main()
