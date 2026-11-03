"""Dukascopy ``*_candles_min_1.bi5`` codec (contracts §13).

A file is LZMA ("alone" format) compressed; each record is 24 bytes big-endian:
``(seconds from 00:00 UTC: u32, open, close, low, high: i32, volume: f32)``.
Prices are integers in units of 10**-price_digits (5 for EURUSD/GBPUSD, 3 for USDJPY).
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass

PARSER_VERSION = "dukascopy-bi5-candles-v1"
_RECORD = struct.Struct(">Iiiiif")
RECORD_SIZE = _RECORD.size  # 24

PRICE_DIGITS = {"EURUSD": 5, "GBPUSD": 5, "USDJPY": 3}


class Bi5Error(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MinuteCandle:
    second_of_day: int
    open: int
    close: int
    low: int
    high: int
    volume: float


def decode(payload: bytes) -> tuple[MinuteCandle, ...]:
    if not payload:
        return ()
    try:
        raw = lzma.decompress(payload, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError as error:
        raise Bi5Error(f"LZMA decode failed: {error}") from error
    if len(raw) % RECORD_SIZE:
        raise Bi5Error(f"truncated payload: {len(raw)} bytes is not a multiple of {RECORD_SIZE}")
    out = []
    for offset in range(0, len(raw), RECORD_SIZE):
        sec, o, c, lo, h, vol = _RECORD.unpack_from(raw, offset)
        out.append(MinuteCandle(sec, o, c, lo, h, vol))
    return tuple(out)


def encode(candles: tuple[MinuteCandle, ...] | list[MinuteCandle]) -> bytes:
    """Inverse of ``decode``; used by tests to build fixture files."""

    raw = b"".join(
        _RECORD.pack(c.second_of_day, c.open, c.close, c.low, c.high, c.volume) for c in candles
    )
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)
