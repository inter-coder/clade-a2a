"""ThreadCache — in-memory thread history sa TTL.

Zamenjuje v1.1.0 `thread_history` SQLite tabelu (vidi samozapazanja §2.6).
Razlog: 95% threadova je 1-2 turn-a, persistencija preko crash-a nije
neophodna (Audit DB cuva istoriju za forenziku). Ako proces crash-uje usred
dugog threada, sledeci ask gubi context — prihvatljivo, ne data loss.

Memorija budget (worst case): 10 msg × ~2KB × 1000 active threadova = ~20MB.
Bezbedno za default Python proces.

TTL semantika: last_access se update-uje na svaki append/get. Thread istice
ako nije dirnut TTL_S sekundi. Eviction je lazy — radi se na svaki access,
O(broj-isteklih) na O(N) where N je broj cached threadova. Za pravu visoku
opterecenost (>10K threadova), zameni dict-om koji ima ordered eviction
(npr. collections.OrderedDict.move_to_end + popleft)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ThreadMsg:
    """Jedna poruka u threadu — sazeta forma podataka korisnih za system prompt."""
    msg_id: str
    ts_ms: int
    direction: str  # "in" | "out"
    peer: str
    kind: str       # "send" | "ask" | "reply"
    payload: dict[str, Any]


@dataclass
class _ThreadEntry:
    messages: deque[ThreadMsg]
    last_access_s: float


class ThreadCache:
    """In-memory thread history sa TTL eviction-om."""

    def __init__(self, ttl_s: float = 3600.0, max_msgs_per_thread: int = 10) -> None:
        self._cache: dict[str, _ThreadEntry] = {}
        self._ttl_s = ttl_s
        self._max = max_msgs_per_thread

    def append(self, thread_id: str, msg: ThreadMsg) -> None:
        """Dodaj poruku u thread. Bounded ringbuffer — najstarija ispada
        ako su vec max_msgs_per_thread u kesu."""
        self._evict_expired()
        entry = self._cache.get(thread_id)
        if entry is None:
            entry = _ThreadEntry(
                messages=deque(maxlen=self._max),
                last_access_s=time.monotonic(),
            )
            self._cache[thread_id] = entry
        entry.messages.append(msg)
        entry.last_access_s = time.monotonic()

    def get_context(self, thread_id: str) -> list[ThreadMsg]:
        """Vrati sve poruke iz threada, hronoloski (najstarija prva).
        Prazan list ako thread ne postoji ili je istekao."""
        self._evict_expired()
        entry = self._cache.get(thread_id)
        if entry is None:
            return []
        entry.last_access_s = time.monotonic()
        return list(entry.messages)

    def has(self, thread_id: str) -> bool:
        self._evict_expired()
        return thread_id in self._cache

    def size(self) -> int:
        """Broj aktivnih thread-ova (posle eviction-a)."""
        self._evict_expired()
        return len(self._cache)

    def clear(self) -> None:
        self._cache.clear()

    def prefill_from_audit(self, audit, thread_id: str) -> None:  # type: ignore[no-untyped-def]
        """Hidracija ThreadCache-a iz Audit DB-a — za scenario kad clade serve
        restartuje i hocemo da imamo thread context za vec-postojece thread-ove.
        Lazy: pozvaj samo kad prvi put treba context za konkretan thread_id.

        Argument `audit` je `clade.audit.Audit` (avoid circular import)."""
        if thread_id in self._cache:
            return
        rows = audit.by_thread(thread_id, max_messages=self._max)
        if not rows:
            return
        msgs = deque(
            (
                ThreadMsg(
                    msg_id=r.msg_id, ts_ms=r.ts_ms, direction=r.direction,
                    peer=r.peer, kind=r.kind, payload=r.payload,
                )
                for r in rows
            ),
            maxlen=self._max,
        )
        self._cache[thread_id] = _ThreadEntry(messages=msgs, last_access_s=time.monotonic())

    # ---- Internal ----

    def _evict_expired(self) -> None:
        if not self._cache:
            return
        now = time.monotonic()
        cutoff = now - self._ttl_s
        # Snapshot keys da izbegnemo RuntimeError sa modifying-while-iterating
        expired = [tid for tid, e in self._cache.items() if e.last_access_s < cutoff]
        for tid in expired:
            del self._cache[tid]
