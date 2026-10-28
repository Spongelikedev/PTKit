"""Packet Tracer container codec (.pkt / .pka / .pkz-style files).

Modern format (Packet Tracer 7.0+), decoding pipeline::

    file bytes
      -> stage 1: reverse byte order, XOR byte i with (L - i*L) & 0xFF
      -> stage 2: Twofish-EAX decrypt (fixed key/nonce, 16-byte tag at end)
      -> stage 3: XOR byte i with (N - i) & 0xFF
      -> stage 4: Qt qUncompress (4-byte big-endian size + zlib stream)
      -> XML

Legacy format (written by Packet Tracer 5.x; PT 8 still reads it, and one
bundled PT 8.1 .pka uses it)::

    file bytes -> XOR byte i with (L - i) & 0xFF -> qUncompress -> XML

Encoding runs the same stages in reverse. Because the key, nonce and
obfuscation are deterministic, re-encoding an unmodified document with the
same zlib parameters reproduces the original file byte-for-byte; the codec
detects those parameters on decode so that round-trips are lossless.

Format reference: mircodz/pka2xml and the ptexplorer project.
"""

from __future__ import annotations

import enum
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .eax import EAX, TagMismatchError
from .twofish import Twofish

# Fixed key material baked into Packet Tracer for activity/network files.
PT_KEY = bytes([137]) * 16
PT_IV = bytes([16]) * 16

_TAG_LEN = 16


class Format(enum.Enum):
    MODERN = "modern"  # PT 7+: Twofish-EAX
    LEGACY = "legacy"  # PT 5.x era: XOR + zlib


class CodecError(Exception):
    """The input is not a decodable Packet Tracer container."""


@dataclass
class ZlibParams:
    level: int = 6
    wbits: int = zlib.MAX_WBITS
    memlevel: int = 8
    strategy: int = zlib.Z_DEFAULT_STRATEGY


@dataclass
class Decoded:
    """Result of decoding a container."""

    xml: bytes
    format: Format
    # zlib parameters that reproduce the original compressed stream, or None
    # if no tried combination matched (re-encoding will still be valid, just
    # not byte-identical).
    zlib_params: ZlibParams | None = None
    original_size: int = 0
    stats: dict = field(default_factory=dict)


# --- byte-level helpers ----------------------------------------------------

def _xor(data: bytes, pad: bytes) -> bytes:
    """XOR ``data`` with ``pad`` (same length) using big-int arithmetic."""
    if not data:
        return b""
    x = int.from_bytes(data, "little") ^ int.from_bytes(pad, "little")
    return x.to_bytes(len(data), "little")


def _periodic(n: int, fn) -> bytes:
    """Build an n-byte pad whose byte i is fn(i) & 0xFF (fn has period 256)."""
    period = bytes(fn(i) & 0xFF for i in range(min(n, 256)))
    reps, rem = divmod(n, 256)
    return period * reps + period[:rem]


def _pad_descending(n: int) -> bytes:
    # byte i = (n - i) & 0xFF
    return _periodic(n, lambda i: n - i)


def _pad_outer(n: int) -> bytes:
    # byte i = (n - i*n) & 0xFF
    return _periodic(n, lambda i: n - i * n)


def _outer_deobfuscate(data: bytes) -> bytes:
    """Stage 1 of decode: out[i] = in[L-1-i] ^ (L - i*L)."""
    return _xor(data[::-1], _pad_outer(len(data)))


def _outer_obfuscate(data: bytes) -> bytes:
    """Inverse of :func:`_outer_deobfuscate`."""
    return _xor(data, _pad_outer(len(data)))[::-1]


# --- Qt qCompress framing --------------------------------------------------

def q_uncompress(data: bytes) -> bytes:
    if len(data) < 4:
        raise CodecError("compressed payload too short")
    (size,) = struct.unpack(">I", data[:4])
    try:
        out = zlib.decompress(data[4:])
    except zlib.error as e:
        raise CodecError(f"zlib decompression failed: {e}") from e
    if len(out) != size:
        raise CodecError(f"size header says {size} bytes, got {len(out)}")
    return out


def q_compress(data: bytes, params: ZlibParams | None = None) -> bytes:
    p = params or ZlibParams()
    c = zlib.compressobj(p.level, zlib.DEFLATED, p.wbits, p.memlevel, p.strategy)
    return struct.pack(">I", len(data)) + c.compress(data) + c.flush()


_CANDIDATE_PARAMS = [ZlibParams(level=l) for l in (6, 9, 1, 2, 3, 4, 5, 7, 8, 0)]


def _detect_zlib_params(xml: bytes, stream: bytes) -> ZlibParams | None:
    for p in _CANDIDATE_PARAMS:
        if q_compress(xml, p) == stream:
            return p
    return None


# --- public API ------------------------------------------------------------

def _cipher() -> EAX:
    return EAX(Twofish(PT_KEY), tag_len=_TAG_LEN)


def decode_modern(data: bytes) -> tuple[bytes, bytes]:
    """Return (xml, compressed_stream) for a PT 7+ container."""
    stage1 = _outer_deobfuscate(data)
    try:
        stage2 = _cipher().decrypt(PT_IV, stage1)
    except TagMismatchError as e:
        raise CodecError("not a PT7+ container (EAX tag mismatch)") from e
    stage3 = _xor(stage2, _pad_descending(len(stage2)))
    return q_uncompress(stage3), stage3


def encode_modern(xml: bytes, params: ZlibParams | None = None) -> bytes:
    compressed = q_compress(xml, params)
    stage3 = _xor(compressed, _pad_descending(len(compressed)))
    stage2 = _cipher().encrypt(PT_IV, stage3)
    return _outer_obfuscate(stage2)


def decode_legacy(data: bytes) -> tuple[bytes, bytes]:
    stream = _xor(data, _pad_descending(len(data)))
    return q_uncompress(stream), stream


def encode_legacy(xml: bytes, params: ZlibParams | None = None) -> bytes:
    compressed = q_compress(xml, params)
    return _xor(compressed, _pad_descending(len(compressed)))


def decode(data: bytes, *, detect_params: bool = True) -> Decoded:
    """Decode container bytes, auto-detecting modern vs legacy format."""
    errors = []
    for fmt, fn in ((Format.MODERN, decode_modern), (Format.LEGACY, decode_legacy)):
        try:
            xml, stream = fn(data)
        except CodecError as e:
            errors.append(f"{fmt.value}: {e}")
            continue
        params = _detect_zlib_params(xml, stream) if detect_params else None
        return Decoded(
            xml=xml,
            format=fmt,
            zlib_params=params,
            original_size=len(data),
            stats={"compressed_size": len(stream) - 4, "xml_size": len(xml)},
        )
    raise CodecError("unrecognised Packet Tracer container; " + "; ".join(errors))


def encode(xml: bytes, fmt: Format = Format.MODERN, params: ZlibParams | None = None) -> bytes:
    if fmt is Format.MODERN:
        return encode_modern(xml, params)
    return encode_legacy(xml, params)


def decode_file(path: str | Path, **kw) -> Decoded:
    return decode(Path(path).read_bytes(), **kw)


def encode_file(path: str | Path, xml: bytes, fmt: Format = Format.MODERN,
                params: ZlibParams | None = None, *, verify: bool = True) -> bytes:
    """Encode and write atomically; with ``verify`` the output is decoded
    again and compared before the target file is replaced."""
    blob = encode(xml, fmt, params)
    if verify:
        check = decode(blob, detect_params=False)
        if check.xml != xml or check.format is not fmt:
            raise CodecError("verification failed: re-decoded XML differs")
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return blob
