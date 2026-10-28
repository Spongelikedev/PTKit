import os

import pytest

from ptkit.eax import EAX, TagMismatchError
from ptkit.twofish import Q0, Q1, Twofish

H = bytes.fromhex


# Known-answer tests from the Twofish paper / ecb_tbl.txt
@pytest.mark.parametrize("key,pt,ct", [
    ("00000000000000000000000000000000", "00000000000000000000000000000000",
     "9F589F5CF6122C32B6BFEC2F2AE8C35A"),
    ("00000000000000000000000000000000", "9F589F5CF6122C32B6BFEC2F2AE8C35A",
     "D491DB16E7B1C39E86CB086B789F5419"),
    ("9F589F5CF6122C32B6BFEC2F2AE8C35A", "D491DB16E7B1C39E86CB086B789F5419",
     "019F9809DE1711858FAAC3A3BA20FBC3"),
    ("0123456789ABCDEFFEDCBA98765432100011223344556677", "00000000000000000000000000000000",
     "CFD1D2E5A9BE9CDF501F13B892BD2248"),
    ("0123456789ABCDEFFEDCBA987654321000112233445566778899AABBCCDDEEFF",
     "00000000000000000000000000000000", "37527BE0052334B89F0CFCCAE87CFA20"),
])
def test_twofish_known_answers(key, pt, ct):
    assert Twofish(H(key)).encrypt_block(H(pt)) == H(ct)


def test_q_permutations_are_bijections():
    assert sorted(Q0) == list(range(256))
    assert sorted(Q1) == list(range(256))
    assert Q0[0] == 0xA9 and Q1[0] == 0x75


def test_twofish_rejects_bad_key_length():
    with pytest.raises(ValueError):
        Twofish(b"short")


class _AesBlock:
    def __init__(self, key):
        from Crypto.Cipher import AES
        self._c = AES.new(key, AES.MODE_ECB)

    def encrypt_block(self, b):
        return self._c.encrypt(b)


@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 32, 33, 255, 1000])
def test_eax_matches_reference_implementation(n):
    """Our generic EAX over AES must match pycryptodome's AES-EAX."""
    from Crypto.Cipher import AES
    key, nonce, header, msg = os.urandom(16), os.urandom(16), os.urandom(n % 37), os.urandom(n)
    ref = AES.new(key, AES.MODE_EAX, nonce=nonce)
    ref.update(header)
    ct, tag = ref.encrypt_and_digest(msg)
    ours = EAX(_AesBlock(key)).encrypt(nonce, msg, header)
    assert ours == ct + tag
    assert EAX(_AesBlock(key)).decrypt(nonce, ours, header) == msg


def test_eax_detects_tampering():
    eax = EAX(Twofish(bytes(16)))
    blob = bytearray(eax.encrypt(bytes(16), b"hello packet tracer"))
    blob[3] ^= 1
    with pytest.raises(TagMismatchError):
        eax.decrypt(bytes(16), bytes(blob))
    with pytest.raises(TagMismatchError):
        eax.decrypt(bytes(16), b"short")
