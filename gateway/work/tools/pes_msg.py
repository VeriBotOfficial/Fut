"""eFootball gate message codec (verified against captured traffic, 2026-10-14).

Wire format:
    request  body = IV(16) || AES-256-CBC( zlib( msgpack(dict) ) )
    response body = IV(16) || AES-256-CBC( gzip( msgpack(dict) ) )
    headers: pes-custom-encrypt: AES256, Content-Type: application/x-www-form-urlencoded

Usage as a library:
    import pes_msg
    req = pes_msg.decode_request(body_bytes)        # -> dict
    body = pes_msg.encode_response(resp_dict)       # -> bytes
"""
from __future__ import annotations

import gzip
import os
import zlib
from pathlib import Path

import msgpack
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[2]

from pes_const import KEY  # noqa: E402  (single source, TASKS O06)


def _pad(data: bytes) -> bytes:
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    if not data or not (1 <= data[-1] <= 16):
        return data
    n = data[-1]
    if data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def aes_cbc_decrypt(iv: bytes, ct: bytes) -> bytes:
    d = Cipher(algorithms.AES(KEY), modes.CBC(iv)).decryptor()
    return d.update(ct) + d.finalize()


def aes_cbc_encrypt(iv: bytes, pt: bytes) -> bytes:
    e = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
    return e.update(pt) + e.finalize()


def decrypt_body(body: bytes) -> bytes:
    """IV-prefixed payload -> plaintext bytes (zlib or gzip)."""
    iv, ct = body[:16], body[16:]
    pt = _unpad(aes_cbc_decrypt(iv, ct))
    if pt[:2] == b"\x1f\x8b":
        return gzip.decompress(pt)
    if pt[:1] == b"\x78":
        return zlib.decompress(pt)
    return pt


def encrypt_body(payload: bytes, use_gzip: bool = True) -> bytes:
    """plaintext bytes -> IV || ciphertext (server side uses gzip)."""
    packed = gzip.compress(payload) if use_gzip else zlib.compress(payload)
    iv = os.urandom(16)
    return iv + aes_cbc_encrypt(iv, _pad(packed))


def decode_request(body: bytes) -> dict:
    return msgpack.unpackb(decrypt_body(body), raw=False, strict_map_key=False)


def decode_response(body: bytes) -> dict:
    return msgpack.unpackb(decrypt_body(body), raw=False, strict_map_key=False)


def encode_response(resp: dict) -> bytes:
    return encrypt_body(msgpack.packb(resp, use_bin_type=True), use_gzip=True)


def encode_request(req: dict) -> bytes:
    """Client-side encoding, for tests."""
    return encrypt_body(msgpack.packb(req, use_bin_type=True), use_gzip=False)
