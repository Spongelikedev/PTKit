"""Unpack/repack loop: the container codec must be an exact inverse pair."""

import os
import struct
import zlib

import pytest

from ptkit import codec
from ptkit.codec import CodecError, Format, ZlibParams


@pytest.mark.parametrize("fmt", [Format.MODERN, Format.LEGACY])
def test_roundtrip_xml(pkt_xml, fmt):
    blob = codec.encode(pkt_xml, fmt)
    d = codec.decode(blob)
    assert d.xml == pkt_xml
    assert d.format is fmt


@pytest.mark.parametrize("fmt", [Format.MODERN, Format.LEGACY])
def test_reencode_is_byte_identical(pkt_xml, fmt):
    """decode -> encode with detected params reproduces the exact file."""
    for level in (1, 6, 9):
        blob = codec.encode(pkt_xml, fmt, ZlibParams(level=level))
        d = codec.decode(blob)
        assert d.zlib_params is not None
        assert codec.encode(d.xml, d.format, d.zlib_params) == blob


@pytest.mark.parametrize("size", [0, 1, 255, 256, 257, 4096, 70_000])
def test_roundtrip_arbitrary_payload_sizes(size):
    # Lengths around 256 exercise the periodic obfuscation pads.
    payload = os.urandom(size)
    for fmt in Format:
        assert codec.decode(codec.encode(payload, fmt)).xml == payload


def test_modern_container_structure(pkt_xml):
    """Undo each documented stage by hand to pin the format down."""
    blob = codec.encode(pkt_xml, Format.MODERN)
    n = len(blob)
    stage1 = bytes(blob[n - 1 - i] ^ ((n - i * n) & 0xFF) for i in range(n))
    plain = codec._cipher().decrypt(codec.PT_IV, stage1)
    m = len(plain)
    stage3 = bytes(plain[i] ^ ((m - i) & 0xFF) for i in range(m))
    assert struct.unpack(">I", stage3[:4])[0] == len(pkt_xml)
    assert zlib.decompress(stage3[4:]) == pkt_xml


def test_obfuscation_helpers_are_inverses():
    data = os.urandom(1000)
    assert codec._outer_deobfuscate(codec._outer_obfuscate(data)) == data


def test_tampered_modern_file_is_rejected(pkt_xml):
    blob = bytearray(codec.encode(pkt_xml, Format.MODERN))
    blob[len(blob) // 2] ^= 0x40
    with pytest.raises(CodecError):
        codec.decode(bytes(blob))


@pytest.mark.parametrize("data", [b"", b"abc", os.urandom(64), b"<PACKETTRACER5/>"])
def test_garbage_is_rejected(data):
    with pytest.raises(CodecError):
        codec.decode(data)


def test_size_header_mismatch_is_rejected(pkt_xml):
    bad = struct.pack(">I", len(pkt_xml) + 1) + zlib.compress(pkt_xml)
    with pytest.raises(CodecError):
        codec.q_uncompress(bad)


def test_encode_file_is_atomic_and_verified(tmp_path, pkt_xml):
    out = tmp_path / "x.pkt"
    codec.encode_file(out, pkt_xml)
    assert codec.decode_file(out).xml == pkt_xml
    assert not (tmp_path / "x.pkt.tmp").exists()
