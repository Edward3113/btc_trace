"""Classify output scripts by how much of the key they put on-chain.

A quantum computer running Shor's algorithm could derive a private key from its public
key. What matters for an output is therefore whether its public key is already
visible on the blockchain:

* P2PK and bare multisig write the public key(s) into the output itself.
* P2TR (Taproot) writes a tweaked public key into the output; a key-path spend needs
  only that key's private key.
* P2PKH, P2WPKH, P2SH and P2WSH write only a hash. The key or script appears when the
  output is spent, so a coin at such an address is exposed only if the address has
  spent before (address reuse), or for the minutes a spend waits to confirm.

This is the "long exposure" classification BIP-360 uses.
"""

from __future__ import annotations

import re

# Script types, as Bitcoin Core names them where it has a name.
P2PK = "pubkey"
P2MS = "multisig"
P2PKH = "pubkeyhash"
P2SH = "scripthash"
P2WPKH = "witness_v0_keyhash"
P2WSH = "witness_v0_scripthash"
P2TR = "witness_v1_taproot"
ANCHOR = "anchor"
NULLDATA = "nulldata"
WITNESS_UNKNOWN = "witness_unknown"
NONSTANDARD = "nonstandard"

KEY_IN_OUTPUT = frozenset({P2PK, P2MS, P2TR})
HASH_ONLY = frozenset({P2PKH, P2SH, P2WPKH, P2WSH})

LABELS = {
    P2PK: "P2PK",
    P2MS: "bare multisig",
    P2PKH: "P2PKH",
    P2SH: "P2SH",
    P2WPKH: "P2WPKH",
    P2WSH: "P2WSH",
    P2TR: "P2TR (Taproot)",
    ANCHOR: "anchor",
    NULLDATA: "OP_RETURN",
    WITNESS_UNKNOWN: "future witness version",
    NONSTANDARD: "nonstandard",
}

_PUSH_KEY = r"(?:21[0-9a-f]{66}|41[0-9a-f]{130})"
_SMALL_INT = r"(?:5[1-9a-f]|60)"  # OP_1 .. OP_16
_PATTERNS = [
    (P2PKH, re.compile(r"76a914[0-9a-f]{40}88ac")),
    (P2SH, re.compile(r"a914[0-9a-f]{40}87")),
    (P2WPKH, re.compile(r"0014[0-9a-f]{40}")),
    (P2WSH, re.compile(r"0020[0-9a-f]{64}")),
    (P2TR, re.compile(r"5120[0-9a-f]{64}")),
    (ANCHOR, re.compile(r"51024e73")),
    (P2PK, re.compile(_PUSH_KEY + r"ac")),
    # OP_m <keys> OP_n OP_CHECKMULTISIG
    (P2MS, re.compile(rf"{_SMALL_INT}{_PUSH_KEY}+{_SMALL_INT}ae")),
    (NULLDATA, re.compile(r"6a[0-9a-f]*")),
]


def script_type(script_hex: str) -> str:
    """The type of an output script given as hex."""
    script = script_hex.lower()
    for name, pattern in _PATTERNS:
        if pattern.fullmatch(script):
            return name
    if _future_witness(script):
        return WITNESS_UNKNOWN
    return NONSTANDARD


def _future_witness(script: str) -> bool:
    """Witness version 2-16 followed by one push of a 2-40 byte program."""
    if len(script) < 8 or not re.fullmatch(r"[0-9a-f]+", script):
        return False
    version = int(script[:2], 16)
    length = int(script[2:4], 16)
    return 0x52 <= version <= 0x60 and 2 <= length <= 40 and len(script) == 4 + 2 * length


def key_in_output(kind: str) -> bool:
    """True when the output script itself reveals a public key."""
    return kind in KEY_IN_OUTPUT


def hash_only(kind: str) -> bool:
    """True when the output holds only a hash until it is spent."""
    return kind in HASH_ONLY
