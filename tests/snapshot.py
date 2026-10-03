"""Write small synthetic UTXO snapshots in Bitcoin Core's dumptxoutset format.

The encoders mirror Bitcoin Core's CompressAmount, WriteVarInt and CompressScript,
so the reader is tested against the real format. The coins are made up.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

MAINNET = b"\xf9\xbe\xb4\xd9"
# The secp256k1 generator point: a real public key, so uncompressed P2PK can round-trip.
G_X = bytes.fromhex("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
G_Y = bytes.fromhex("483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8")


def compress_amount(n: int) -> int:
    if n == 0:
        return 0
    e = 0
    while n % 10 == 0 and e < 9:
        n //= 10
        e += 1
    if e < 9:
        d = n % 10
        n //= 10
        return 1 + (n * 9 + d - 1) * 10 + e
    return 1 + (n - 1) * 10 + 9


def varint(n: int) -> bytes:
    out = []
    while True:
        out.append((n & 0x7F) | (0x80 if out else 0))
        if n <= 0x7F:
            break
        n = (n >> 7) - 1
    return bytes(reversed(out))


def compactsize(n: int) -> bytes:
    if n < 253:
        return bytes((n,))
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    return b"\xfe" + n.to_bytes(4, "little")


def compress_script(script: bytes) -> bytes:
    if len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[23:] == b"\x88\xac":
        return varint(0) + script[3:23]
    if len(script) == 23 and script[:2] == b"\xa9\x14" and script[22] == 0x87:
        return varint(1) + script[2:22]
    if len(script) == 35 and script[0] == 33 and script[34] == 0xAC and script[1] in (2, 3):
        return varint(script[1]) + script[2:34]
    if len(script) == 67 and script[0] == 65 and script[1] == 4 and script[66] == 0xAC:
        y_odd = script[65] & 1
        return varint(4 + y_odd) + script[2:34]
    return varint(len(script) + 6) + script


@dataclass
class Coin:
    txid: bytes  # internal byte order
    vout: int
    height: int
    coinbase: bool
    sats: int
    script: bytes

    def serialized(self) -> bytes:
        """The coin as Bitcoin Core hashes it (TxOutSer)."""
        code = self.height * 2 + int(self.coinbase)
        return (
            self.txid
            + self.vout.to_bytes(4, "little")
            + code.to_bytes(4, "little")
            + self.sats.to_bytes(8, "little")
            + compactsize(len(self.script))
            + self.script
        )


def write_snapshot(coins: list[Coin], base_hash: bytes = b"\x11" * 32) -> bytes:
    """Coins must be grouped by txid, as Bitcoin Core writes them."""
    out = bytearray(b"utxo\xff" + (2).to_bytes(2, "little") + MAINNET + base_hash)
    out += len(coins).to_bytes(8, "little")
    groups: list[list[Coin]] = []
    for c in coins:
        if groups and groups[-1][0].txid == c.txid:
            groups[-1].append(c)
        else:
            groups.append([c])
    for group in groups:
        out += group[0].txid + compactsize(len(group))
        for c in group:
            out += compactsize(c.vout) + varint(c.height * 2 + int(c.coinbase))
            out += varint(compress_amount(c.sats)) + compress_script(c.script)
    return bytes(out)


def expected_hash(coins: list[Coin]) -> str:
    """Bitcoin Core's hash_serialized_3, computed directly from the coins."""
    h = hashlib.sha256(b"".join(c.serialized() for c in coins)).digest()
    return hashlib.sha256(h).digest()[::-1].hex()


def p2pkh(n: int) -> bytes:
    return b"\x76\xa9\x14" + bytes([n]) * 20 + b"\x88\xac"


def p2sh(n: int) -> bytes:
    return b"\xa9\x14" + bytes([n]) * 20 + b"\x87"


def p2wpkh(n: int) -> bytes:
    return b"\x00\x14" + bytes([n]) * 20


def p2wsh(n: int) -> bytes:
    return b"\x00\x20" + bytes([n]) * 32


def p2tr(n: int) -> bytes:
    return b"\x51\x20" + bytes([n]) * 32


def p2pk_compressed() -> bytes:
    return b"\x21\x02" + G_X + b"\xac"


def p2pk_uncompressed() -> bytes:
    return b"\x41\x04" + G_X + G_Y + b"\xac"


def bare_multisig() -> bytes:
    key = b"\x21\x02" + G_X
    return b"\x51" + key + key + b"\x52\xae"
