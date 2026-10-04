"""Build serialized transactions and blocks for tests of the reveal scan.

Signatures are placeholders of the right shape; nothing here is checked by a node.
The public keys are real secp256k1 points so their hashes match known addresses.
"""

from __future__ import annotations

from tests.snapshot import G_X, G_Y, compactsize

# Private key 1's public key, compressed and uncompressed.
KEY1 = b"\x02" + G_X
KEY1_FULL = b"\x04" + G_X + G_Y
# A second real point: 2G.
KEY2 = bytes.fromhex("02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5")
SIG = b"\x30\x44" + b"\x02\x20" + b"\x11" * 32 + b"\x02\x20" + b"\x22" * 32 + b"\x01"
SCHNORR = b"\x33" * 64


def push(data: bytes) -> bytes:
    if len(data) < 76:
        return bytes((len(data),)) + data
    if len(data) < 256:
        return b"\x4c" + bytes((len(data),)) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def multisig(*keys: bytes, m: int = 1) -> bytes:
    return (
        bytes((0x50 + m,)) + b"".join(push(k) for k in keys) + bytes((0x50 + len(keys),)) + b"\xae"
    )


def tx(inputs: list[tuple[bytes, list[bytes]]], n_outputs: int = 1) -> bytes:
    """inputs: (scriptSig, witness items). Segwit serialization if any witness is set."""
    segwit = any(w for _, w in inputs)
    out = b"\x02\x00\x00\x00" + (b"\x00\x01" if segwit else b"")
    out += compactsize(len(inputs))
    for i, (script_sig, _) in enumerate(inputs):
        out += bytes([i + 1]) * 32 + b"\x00\x00\x00\x00"
        out += compactsize(len(script_sig)) + script_sig + b"\xff\xff\xff\xff"
    out += compactsize(n_outputs)
    for _ in range(n_outputs):
        script = b"\x00\x14" + b"\x55" * 20
        out += (1000).to_bytes(8, "little") + compactsize(len(script)) + script
    if segwit:
        for _, witness in inputs:
            out += compactsize(len(witness))
            for item in witness:
                out += compactsize(len(item)) + item
    return out + b"\x00\x00\x00\x00"


COINBASE = tx([(b"\x03\x01\x02\x03", [])])


def block(*txs: bytes) -> bytes:
    body = [COINBASE, *txs]
    return b"\x00" * 80 + compactsize(len(body)) + b"".join(body)


class RawSource:
    """Blocks by height, served like a node: hashes, then raw blocks by hash."""

    def __init__(self, blocks: dict[int, bytes]) -> None:
        self.blocks = blocks
        self.fetched: list[int] = []

    @staticmethod
    def hash_of(height: int) -> str:
        return f"{height:064x}"

    def height_of(self, blockhash: str) -> int:
        return int(blockhash, 16)

    def block_hashes(self, start: int, end: int) -> list[str]:
        return [self.hash_of(h) for h in range(start, end + 1)]

    def raw_block(self, blockhash: str) -> bytes:
        height = int(blockhash, 16)
        self.fetched.append(height)
        return self.blocks.get(height, block())
