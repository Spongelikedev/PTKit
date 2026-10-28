"""EAX authenticated-encryption mode over any 128-bit block cipher.

Implements Bellare/Rogaway/Wagner EAX as used by Crypto++'s ``EAX<T>``:
OMAC1 (CMAC) for the nonce/header/ciphertext tags and a 128-bit big-endian
CTR keystream seeded with the nonce tag. The cipher object only needs an
``encrypt_block(bytes) -> bytes`` method.
"""

from __future__ import annotations

import hmac

_BS = 16
_MASK128 = (1 << 128) - 1


class TagMismatchError(ValueError):
    """Raised when an EAX authentication tag does not verify."""


def _dbl(block: int) -> int:
    block <<= 1
    if block >> 128:
        block = (block & _MASK128) ^ 0x87
    return block


class EAX:
    def __init__(self, cipher, tag_len: int = 16):
        if not 1 <= tag_len <= _BS:
            raise ValueError("tag_len must be between 1 and 16")
        self._enc = cipher.encrypt_block
        self.tag_len = tag_len
        l = int.from_bytes(self._enc(bytes(_BS)), "big")
        self._k1 = _dbl(l)
        self._k2 = _dbl(self._k1)

    def _omac(self, t: int, data: bytes) -> bytes:
        enc = self._enc
        msg = t.to_bytes(_BS, "big") + data  # always >= one full block
        n_full = len(msg) // _BS
        if len(msg) % _BS == 0:
            last = int.from_bytes(msg[-_BS:], "big") ^ self._k1
            body_blocks = n_full - 1
        else:
            tail = msg[n_full * _BS :] + b"\x80"
            tail += bytes(_BS - len(tail))
            last = int.from_bytes(tail, "big") ^ self._k2
            body_blocks = n_full
        state = 0
        for i in range(body_blocks):
            blk = int.from_bytes(msg[i * _BS : (i + 1) * _BS], "big")
            state = int.from_bytes(enc((state ^ blk).to_bytes(_BS, "big")), "big")
        return enc((state ^ last).to_bytes(_BS, "big"))

    def _ctr(self, nonce_tag: bytes, data: bytes) -> bytes:
        if not data:
            return b""
        enc = self._enc
        ctr = int.from_bytes(nonce_tag, "big")
        n_blocks = -(-len(data) // _BS)
        stream = b"".join(
            enc(((ctr + i) & _MASK128).to_bytes(_BS, "big")) for i in range(n_blocks)
        )[: len(data)]
        x = int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")
        return x.to_bytes(len(data), "big")

    def encrypt(self, nonce: bytes, plaintext: bytes, header: bytes = b"") -> bytes:
        """Return ``ciphertext || tag``."""
        n = self._omac(0, nonce)
        h = self._omac(1, header)
        c = self._ctr(n, plaintext)
        cm = self._omac(2, c)
        tag = bytes(a ^ b ^ d for a, b, d in zip(n, h, cm))[: self.tag_len]
        return c + tag

    def decrypt(self, nonce: bytes, data: bytes, header: bytes = b"") -> bytes:
        """Verify and decrypt ``ciphertext || tag``; raise on tag mismatch."""
        if len(data) < self.tag_len:
            raise TagMismatchError("input shorter than authentication tag")
        c, tag = data[: -self.tag_len], data[-self.tag_len :]
        n = self._omac(0, nonce)
        h = self._omac(1, header)
        cm = self._omac(2, c)
        expect = bytes(a ^ b ^ d for a, b, d in zip(n, h, cm))[: self.tag_len]
        if not hmac.compare_digest(expect, tag):
            raise TagMismatchError("EAX authentication tag mismatch")
        return self._ctr(n, c)
