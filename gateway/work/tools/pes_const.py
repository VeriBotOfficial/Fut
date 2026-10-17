"""Protocol constants — the single source of truth (TASKS O06).

The eFootball gate crypto envelope's static AES-256 key used to be duplicated
byte-for-byte in ``pes_crypto.py`` and ``pes_msg.py``. Any future rotation (or a
typo fix) had to be applied twice, and a divergence would silently break one
side while the other kept passing its own tests. This module is now the only
place that holds the literal; both files import from here.

Verified key provenance (2026-10-14): extracted from ``AES_set_encrypt_key`` via
Frida on ``jp.konami.pesam`` 11.1.0; the 110/110 round-trip in
``pes_crypto.py --test`` is the acceptance gate.

Wire-level facts that belong to the protocol (not to any one tool):

  key       32-byte static AES key (below)
  IV        per-message, 16 bytes, prefixed on the body OR carried in `sign`
  body      request  = IV || AES-256-CBC( zlib( msgpack(dict) ) )
            response = IV || AES-256-CBC( gzip( msgpack(dict) ) )
  headers   pes-custom-encrypt: AES256
"""
from __future__ import annotations

# The literal below is the ONE copy in the codebase. check_docs.py exempts this
# file from the "no duplicated constants" spirit the same way it exempts
# toolpaths.py for tool binaries.
KEY_HEX = (
    "43740981523cdc171e71de2ccab1a5a9"
    "b86f4b833196c55facd4bd25846c33f5"
)

KEY: bytes = bytes.fromhex(KEY_HEX)

IV_SIZE = 16
KEY_SIZE = 32
ENCRYPT_HEADER = "AES256"

assert len(KEY) == KEY_SIZE, f"static key must be {KEY_SIZE} bytes, got {len(KEY)}"
