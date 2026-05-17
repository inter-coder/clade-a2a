"""Storage backend za relay state — nonce dedup + inbox queue.

Dva implementacija:
- InMemoryStore: process-local dict, gubi sve na restart. Faza 0/1 default.
- RedisStore: persistent, preživljava restart. Faza 2 production.

Bira se preko REDIS_URL env vara. Ako nije setovan ili Redis nije dostupan,
fallback na InMemory sa upozorenjem u log-u.

Pending asks (in-flight clade_ask blocking) OSTAJU u memoriji — asyncio.Future
objekti nisu serijabilni, a ako relay restartuje sve asks ionako fail-uju
(klijent dobija HTTP gresku). To je prihvatljivo: clade_ask je sinhroni,
korisnik moze samo retry.
"""

import json
import os
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any


class Store(ABC):
    """Apstraktni storage interfejs."""

    @abstractmethod
    async def check_nonce(self, nonce: str, ttl_s: int) -> bool:
        """True ako je nonce nov i sad zabelezen; False ako je vec videno (replay)."""

    @abstractmethod
    async def inbox_push(self, agent_id: str, envelope: dict, max_size: int) -> bool:
        """Vrati True ako je dodato, False ako je inbox pun."""

    @abstractmethod
    async def inbox_drain(self, agent_id: str, max_items: int) -> list[dict]:
        """Vadi i vraca do max_items poruka."""

    @abstractmethod
    async def inbox_size(self, agent_id: str) -> int: ...

    @abstractmethod
    async def health(self) -> dict[str, Any]: ...


class InMemoryStore(Store):
    """Process-local dict. Faza 0/1 default, idealno za local dev."""

    def __init__(self) -> None:
        self._inbox: dict[str, list[dict]] = defaultdict(list)
        self._nonces: dict[str, float] = {}

    async def check_nonce(self, nonce: str, ttl_s: int) -> bool:
        now = time.time()
        if len(self._nonces) > 10_000:
            expired = [n for n, exp in self._nonces.items() if exp < now]
            for n in expired:
                self._nonces.pop(n, None)
        if nonce in self._nonces:
            return False
        self._nonces[nonce] = now + ttl_s
        return True

    async def inbox_push(self, agent_id: str, envelope: dict, max_size: int) -> bool:
        if len(self._inbox[agent_id]) >= max_size:
            return False
        self._inbox[agent_id].append(envelope)
        return True

    async def inbox_drain(self, agent_id: str, max_items: int) -> list[dict]:
        items = self._inbox[agent_id][:max_items]
        del self._inbox[agent_id][:max_items]
        return items

    async def inbox_size(self, agent_id: str) -> int:
        return len(self._inbox[agent_id])

    async def health(self) -> dict[str, Any]:
        return {
            "backend": "in-memory",
            "inbox_sizes": {k: len(v) for k, v in self._inbox.items() if v},
            "nonce_cache_size": len(self._nonces),
        }


class RedisStore(Store):
    """Production backend. Persistuje izmedju restart-ova relay-a.

    Schema:
        nonce:<nonce>       — string "1" sa EX = ttl_s (atomic SET NX EX)
        inbox:<agent_id>    — LIST sa JSON-serijabilnim envelope-ima (RPUSH/LPOP)
    """

    def __init__(self, redis_client: Any) -> None:
        self._r = redis_client

    async def check_nonce(self, nonce: str, ttl_s: int) -> bool:
        # SET NX EX: atomicno, vraca True samo ako je kljuc bio nov
        result = await self._r.set(f"nonce:{nonce}", "1", nx=True, ex=ttl_s)
        return bool(result)

    async def inbox_push(self, agent_id: str, envelope: dict, max_size: int) -> bool:
        key = f"inbox:{agent_id}"
        size = await self._r.llen(key)
        if size >= max_size:
            return False
        await self._r.rpush(key, json.dumps(envelope))
        # Inbox TTL — 24h, refresh-uje se svakim push-em
        await self._r.expire(key, 86_400)
        return True

    async def inbox_drain(self, agent_id: str, max_items: int) -> list[dict]:
        key = f"inbox:{agent_id}"
        items = []
        # LPOP COUNT je dostupan od Redis 6.2+; fallback na petlju
        try:
            raw = await self._r.lpop(key, max_items)
            if raw is None:
                return []
            if isinstance(raw, list):
                items = [json.loads(x) for x in raw]
            else:
                items = [json.loads(raw)]
        except Exception:
            # Fallback: pojedinacni LPOP-ovi
            for _ in range(max_items):
                raw = await self._r.lpop(key)
                if raw is None:
                    break
                items.append(json.loads(raw))
        return items

    async def inbox_size(self, agent_id: str) -> int:
        return await self._r.llen(f"inbox:{agent_id}")

    async def health(self) -> dict[str, Any]:
        try:
            await self._r.ping()
            redis_ok = True
        except Exception as e:
            redis_ok = False
        # Brzo: lista svih inbox:* kljuceva sa SCAN
        sizes: dict[str, int] = {}
        async for key in self._r.scan_iter(match="inbox:*"):
            agent = key.decode() if isinstance(key, bytes) else key
            agent = agent.split(":", 1)[1]
            n = await self._r.llen(f"inbox:{agent}")
            if n > 0:
                sizes[agent] = n
        return {
            "backend": "redis",
            "redis_ok": redis_ok,
            "inbox_sizes": sizes,
        }


async def make_store(redis_url: str | None = None) -> Store:
    """Factory: ako REDIS_URL setovan i Redis dostupan → RedisStore,
    inace InMemoryStore sa upozorenjem."""
    url = redis_url or os.environ.get("REDIS_URL")
    if not url:
        print("[store] REDIS_URL not set, using in-memory store (state lost on restart)")
        return InMemoryStore()
    try:
        import redis.asyncio as redis  # noqa: PLC0415
        client = redis.from_url(url, decode_responses=False)
        await client.ping()
        print(f"[store] connected to Redis at {url}")
        return RedisStore(client)
    except Exception as e:
        print(f"[store] WARNING: failed to connect to Redis ({e}); falling back to in-memory")
        return InMemoryStore()
