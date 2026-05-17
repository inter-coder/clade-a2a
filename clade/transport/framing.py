"""Length-prefixed JSON framing za stream transporte.

Format: `[4-byte big-endian length][JSON bytes]`. Bez delimiter-a, bez
escape sequence-a — duzina je eksplicitna. Pojednostavljen "newline-delimited
JSON" pristup, robusniji u prisustvu newline-ova u payload-u."""

from __future__ import annotations

import asyncio

MAX_FRAME_BYTES = 4 * 1024 * 1024  # 4 MB — peer poruke su uvek mnogo manje
LENGTH_PREFIX_BYTES = 4


class FrameTooLarge(Exception):
    """Frame veci od MAX_FRAME_BYTES — verovatno bug ili napad. Zatvori konekciju."""


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Posalji 4-byte length + payload bytes. Drain pre povratka."""
    if len(data) > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"frame {len(data)} > MAX_FRAME_BYTES {MAX_FRAME_BYTES}")
    writer.write(len(data).to_bytes(LENGTH_PREFIX_BYTES, "big"))
    writer.write(data)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Procitaj jedan frame. Baca FrameTooLarge ako prefix kaze previse;
    `asyncio.IncompleteReadError` ako peer zatvori usred frame-a."""
    length_bytes = await reader.readexactly(LENGTH_PREFIX_BYTES)
    length = int.from_bytes(length_bytes, "big")
    if length > MAX_FRAME_BYTES:
        raise FrameTooLarge(f"announced frame {length} > MAX_FRAME_BYTES {MAX_FRAME_BYTES}")
    return await reader.readexactly(length)
