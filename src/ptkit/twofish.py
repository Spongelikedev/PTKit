"""Pure-Python Twofish block cipher (encryption direction only).

Packet Tracer 7+ encrypts its containers with Twofish in EAX mode. EAX only
ever uses the forward block transform (CTR + OMAC), so decryption of single
blocks is not implemented.

Reference: Schneier et al., "Twofish: A 128-Bit Block Cipher" (1998).
Key-dependent S-boxes are fully expanded into four 256-entry tables at key
setup so each round costs eight table lookups.
"""

from __future__ import annotations

import struct

_M32 = 0xFFFFFFFF

# 4-bit permutation tables for q0 and q1 (Twofish paper, section 4.3.5).
_Q0_T = (
    (0x8, 0x1, 0x7, 0xD, 0x6, 0xF, 0x3, 0x2, 0x0, 0xB, 0x5, 0x9, 0xE, 0xC, 0xA, 0x4),
    (0xE, 0xC, 0xB, 0x8, 0x1, 0x2, 0x3, 0x5, 0xF, 0x4, 0xA, 0x6, 0x7, 0x0, 0x9, 0xD),
    (0xB, 0xA, 0x5, 0xE, 0x6, 0xD, 0x9, 0x0, 0xC, 0x8, 0xF, 0x3, 0x2, 0x4, 0x7, 0x1),
    (0xD, 0x7, 0xF, 0x4, 0x1, 0x2, 0x6, 0xE, 0x9, 0xB, 0x3, 0x0, 0x8, 0x5, 0xC, 0xA),
)
_Q1_T = (
    (0x2, 0x8, 0xB, 0xD, 0xF, 0x7, 0x6, 0xE, 0x3, 0x1, 0x9, 0x4, 0x0, 0xA, 0xC, 0x5),
    (0x1, 0xE, 0x2, 0xB, 0x4, 0xC, 0x3, 0x7, 0x6, 0xD, 0xA, 0x5, 0xF, 0x9, 0x0, 0x8),
    (0x4, 0xC, 0x7, 0x5, 0x1, 0x6, 0x9, 0xA, 0x0, 0xE, 0xD, 0x8, 0x2, 0xB, 0x3, 0xF),
    (0xB, 0x9, 0x5, 0x1, 0xC, 0x3, 0xD, 0xE, 0x6, 0x4, 0x7, 0xF, 0x2, 0x0, 0x8, 0xA),
)


def _ror4(x: int, n: int) -> int:
    return ((x >> n) | (x << (4 - n))) & 0xF


def _build_q(t) -> bytes:
    out = bytearray(256)
    for x in range(256):
        a0, b0 = x >> 4, x & 0xF
        a1 = a0 ^ b0
        b1 = a0 ^ _ror4(b0, 1) ^ ((8 * a0) & 0xF)
        a2, b2 = t[0][a1], t[1][b1]
        a3 = a2 ^ b2
        b3 = a2 ^ _ror4(b2, 1) ^ ((8 * a2) & 0xF)
        a4, b4 = t[2][a3], t[3][b3]
        out[x] = (b4 << 4) | a4
    return bytes(out)


Q0 = _build_q(_Q0_T)
Q1 = _build_q(_Q1_T)


def _gf_mul(a: int, b: int, poly: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= poly
        b >>= 1
    return r


_MDS_POLY = 0x169
_RS_POLY = 0x14D

_MDS = (
    (0x01, 0xEF, 0x5B, 0x5B),
    (0x5B, 0xEF, 0xEF, 0x01),
    (0xEF, 0x5B, 0x01, 0xEF),
    (0xEF, 0x01, 0xEF, 0x5B),
)

_RS = (
    (0x01, 0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E),
    (0xA4, 0x56, 0x82, 0xF3, 0x1E, 0xC6, 0x68, 0xE5),
    (0x02, 0xA1, 0xFC, 0xC1, 0x47, 0xAE, 0x3D, 0x19),
    (0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E, 0x03),
)

# _MDS_COL[j][v] = column j of the MDS matrix multiplied by byte v, packed
# little-endian into a 32-bit word.
_MDS_COL = tuple(
    tuple(
        sum(_gf_mul(_MDS[i][j], v, _MDS_POLY) << (8 * i) for i in range(4))
        for v in range(256)
    )
    for j in range(4)
)

# Order of q-permutations applied to each byte lane, outermost stage last.
# Stage index 0 is the final q applied; index k-1 is the first.
_QORD = (
    (Q1, Q0, Q1, Q0),  # applied after XOR with L0 (final)
    (Q0, Q0, Q1, Q1),  # after XOR with L1
    (Q0, Q1, Q0, Q1),  # first q for k == 2
    (Q1, Q1, Q0, Q0),  # first q for k == 3 (before XOR with L2)
    (Q1, Q0, Q0, Q1),  # first q for k == 4 (before XOR with L3)
)


def _sbox_lane(x: int, lane: int, key_bytes: list[list[int]]) -> int:
    """Run byte ``x`` of lane ``lane`` through the key-dependent q-chain.

    ``key_bytes[i][lane]`` is byte ``lane`` of key word L_i.
    """
    k = len(key_bytes)
    if k == 4:
        x = _QORD[4][lane][x] ^ key_bytes[3][lane]
    if k >= 3:
        x = _QORD[3][lane][x] ^ key_bytes[2][lane]
    x = _QORD[2][lane][x] ^ key_bytes[1][lane]
    x = _QORD[1][lane][x] ^ key_bytes[0][lane]
    return _QORD[0][lane][x]


def _h(x: int, words: list[int]) -> int:
    kb = [[(w >> (8 * j)) & 0xFF for j in range(4)] for w in words]
    out = 0
    for lane in range(4):
        out ^= _MDS_COL[lane][_sbox_lane((x >> (8 * lane)) & 0xFF, lane, kb)]
    return out


def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & _M32


def _ror(x: int, n: int) -> int:
    return ((x >> n) | (x << (32 - n))) & _M32


class Twofish:
    """Twofish with a 128/192/256-bit key. Only ``encrypt_block`` is provided."""

    block_size = 16

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("Twofish key must be 16, 24 or 32 bytes")
        k = len(key) // 8
        m = struct.unpack("<%dI" % (2 * k), key)
        me = [m[2 * i] for i in range(k)]
        mo = [m[2 * i + 1] for i in range(k)]

        s = []
        for i in range(k):
            chunk = key[8 * i : 8 * i + 8]
            word = 0
            for r in range(4):
                b = 0
                for c in range(8):
                    b ^= _gf_mul(_RS[r][c], chunk[c], _RS_POLY)
                word |= b << (8 * r)
            s.append(word)
        s.reverse()  # S = (S_{k-1}, ..., S_0)

        rho = 0x01010101
        sk = []
        for i in range(20):
            a = _h((2 * i * rho) & _M32, me)
            b = _rol(_h(((2 * i + 1) * rho) & _M32, mo), 8)
            sk.append((a + b) & _M32)
            sk.append(_rol((a + 2 * b) & _M32, 9))
        self._k = tuple(sk)

        kb = [[(w >> (8 * j)) & 0xFF for j in range(4)] for w in s]
        self._t = tuple(
            tuple(_MDS_COL[lane][_sbox_lane(x, lane, kb)] for x in range(256))
            for lane in range(4)
        )

    def encrypt_block(self, block: bytes) -> bytes:
        t0, t1, t2, t3 = self._t
        k = self._k
        r0, r1, r2, r3 = struct.unpack("<4I", block)
        r0 ^= k[0]
        r1 ^= k[1]
        r2 ^= k[2]
        r3 ^= k[3]
        for rnd in range(0, 16, 2):
            # two rounds per iteration, avoiding the explicit swap
            x = t0[r0 & 0xFF] ^ t1[(r0 >> 8) & 0xFF] ^ t2[(r0 >> 16) & 0xFF] ^ t3[r0 >> 24]
            y = t0[r1 >> 24] ^ t1[r1 & 0xFF] ^ t2[(r1 >> 8) & 0xFF] ^ t3[(r1 >> 16) & 0xFF]
            r2 ^= (x + y + k[2 * rnd + 8]) & _M32
            r2 = ((r2 >> 1) | (r2 << 31)) & _M32
            r3 = (((r3 << 1) | (r3 >> 31)) & _M32) ^ ((x + 2 * y + k[2 * rnd + 9]) & _M32)

            x = t0[r2 & 0xFF] ^ t1[(r2 >> 8) & 0xFF] ^ t2[(r2 >> 16) & 0xFF] ^ t3[r2 >> 24]
            y = t0[r3 >> 24] ^ t1[r3 & 0xFF] ^ t2[(r3 >> 8) & 0xFF] ^ t3[(r3 >> 16) & 0xFF]
            r0 ^= (x + y + k[2 * rnd + 10]) & _M32
            r0 = ((r0 >> 1) | (r0 << 31)) & _M32
            r1 = (((r1 << 1) | (r1 >> 31)) & _M32) ^ ((x + 2 * y + k[2 * rnd + 11]) & _M32)
        return struct.pack("<4I", r2 ^ k[4], r3 ^ k[5], r0 ^ k[6], r1 ^ k[7])
