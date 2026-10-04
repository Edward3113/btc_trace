"""Find every hash-based address that has revealed its key or script by spending.

A P2PKH, P2WPKH, P2SH or P2WSH output shows only a hash. Spending it puts the
preimage on-chain: the public key (P2PKH, P2WPKH) or the redeem/witness script
(P2SH, P2WSH), which in turn usually contains public keys. From then on, any coins
still held at that address, or sent to it later, have a visible key.

Bitcoin Core has no index of this, so the scan reads every block once. It does not
need to know which output each input spends: hashing what the input reveals gives back
the address it came from.

* A public key revealed in a scriptSig or witness exposes hash160(key): the P2PKH and
  P2WPKH addresses of that key. For a compressed key it also exposes the P2SH-wrapped
  P2WPKH address hash160(0x0014 || hash160(key)). An uncompressed key exposes the
  compressed form's addresses too, since it is the same key.
* A redeem script (the last push of a P2SH scriptSig) exposes hash160(script).
* A witness script (the last P2WSH witness item) exposes sha256(script), and
  hash160(0x0020 || sha256(script)) when it is wrapped in P2SH.
* Public keys inside revealed scripts (multisig and the like) are revealed too.

Taproot spends are skipped (Taproot outputs show a key anyway), as are P2PK spends,
whose scriptSig holds only a signature.

To keep memory small, hashes are compared by their first 8 bytes as unsigned 64-bit
integers. With tens of millions of target addresses and billions of comparisons, the
expected number of false matches is well below one.

Known limit: a compressed key revealed by a spend does not mark the P2PKH address of
the same key in uncompressed form (that would need an elliptic-curve square root for
every key). Such outputs are rare and old.
"""

from __future__ import annotations

import hashlib
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

KEY_SIZES = {33: (2, 3), 65: (4,)}


def ripemd160_available() -> bool:
    try:
        hashlib.new("ripemd160", b"")
    except ValueError:
        return False
    return True


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def prefix(digest: bytes) -> int:
    """The first 8 bytes of a hash as an unsigned integer: the form used for matching."""
    return int.from_bytes(digest[:8], "big")


def is_pubkey(item: bytes) -> bool:
    prefixes = KEY_SIZES.get(len(item))
    return prefixes is not None and item[0] in prefixes


def looks_like_signature(item: bytes) -> bool:
    """A DER-encoded ECDSA signature plus sighash byte."""
    return 9 <= len(item) <= 73 and item[0] == 0x30 and item[1] == len(item) - 3


def script_pushes(script: bytes) -> list[bytes]:
    """The data pushes in a script, ignoring other opcodes; stops at a malformed push."""
    pushes = []
    i, n = 0, len(script)
    while i < n:
        op = script[i]
        i += 1
        if op == 0:
            pushes.append(b"")
            continue
        if op <= 75:
            size = op
        elif op == 76:
            if i + 1 > n:
                break
            size = script[i]
            i += 1
        elif op == 77:
            if i + 2 > n:
                break
            size = int.from_bytes(script[i : i + 2], "little")
            i += 2
        elif op == 78:
            if i + 4 > n:
                break
            size = int.from_bytes(script[i : i + 4], "little")
            i += 4
        else:
            continue
        if i + size > n:
            break
        pushes.append(script[i : i + size])
        i += size
    return pushes


def key_reveals(key: bytes, out: list[bytes]) -> None:
    """Hashes of the addresses a revealed public key exposes."""
    h = hash160(key)
    out.append(h)
    if len(key) == 65:
        compressed = bytes((2 + (key[64] & 1),)) + key[1:33]
        h = hash160(compressed)
        out.append(h)
    out.append(hash160(b"\x00\x14" + h))  # P2SH-wrapped P2WPKH of the compressed key


def script_reveals(script: bytes, out: list[bytes]) -> None:
    """Keys pushed inside a revealed script."""
    for item in script_pushes(script):
        if is_pubkey(item):
            key_reveals(item, out)


def _is_taproot_spend(witness: list[bytes], script_sig: bytes) -> bool:
    if script_sig:
        return False
    items = witness
    if len(items) >= 2 and items[-1][:1] == b"\x50":  # annex
        items = items[:-1]
    if len(items) == 1:
        return len(items[0]) in (64, 65)  # key path: one Schnorr signature
    control = items[-1]
    return len(control) >= 33 and (len(control) - 33) % 32 == 0 and control[0] & 0xFE == 0xC0


def input_reveals(script_sig: bytes, witness: list[bytes], out: list[bytes]) -> None:
    """Append the hashes of every address this input's spend reveals."""
    if witness:
        if len(witness) == 2 and is_pubkey(witness[1]):
            key_reveals(witness[1], out)  # P2WPKH, or P2SH-wrapped P2WPKH
            return
        if _is_taproot_spend(witness, script_sig):
            return
        witness_script = witness[-1]
        program = hashlib.sha256(witness_script).digest()
        out.append(program)  # P2WSH
        if script_sig:
            out.append(hash160(b"\x00\x20" + program))  # P2SH-wrapped P2WSH
        script_reveals(witness_script, out)
        return
    if not script_sig:
        return
    pushes = script_pushes(script_sig)
    if not pushes:
        return
    last = pushes[-1]
    if is_pubkey(last):
        key_reveals(last, out)  # P2PKH
    elif last and not looks_like_signature(last):
        out.append(hash160(last))  # P2SH redeem script
        script_reveals(last, out)


@dataclass
class BlockCounts:
    inputs: int = 0
    reveals: int = 0


def _compactsize(data: bytes, pos: int) -> tuple[int, int]:
    n = data[pos]
    if n < 253:
        return n, pos + 1
    width = (2, 4, 8)[n - 253]
    return int.from_bytes(data[pos + 1 : pos + 1 + width], "little"), pos + 1 + width


def block_inputs(raw: bytes) -> Iterator[tuple[bytes, list[bytes]]]:
    """(scriptSig, witness items) for every non-coinbase input of a serialized block."""
    pos = 80
    tx_count, pos = _compactsize(raw, pos)
    for t in range(tx_count):
        pos += 4  # version
        segwit = raw[pos] == 0 and raw[pos + 1] == 1
        if segwit:
            pos += 2
        n_in, pos = _compactsize(raw, pos)
        sigs = []
        for _ in range(n_in):
            pos += 36  # previous output
            size, pos = _compactsize(raw, pos)
            sigs.append(raw[pos : pos + size])
            pos += size + 4  # script and sequence
        n_out, pos = _compactsize(raw, pos)
        for _ in range(n_out):
            size, pos = _compactsize(raw, pos + 8)
            pos += size
        witnesses: list[list[bytes]] = []
        if segwit:
            for _ in range(n_in):
                items, pos = _compactsize(raw, pos)
                stack = []
                for _ in range(items):
                    size, pos = _compactsize(raw, pos)
                    stack.append(raw[pos : pos + size])
                    pos += size
                witnesses.append(stack)
        pos += 4  # locktime
        if t == 0:
            continue  # coinbase
        for i, sig in enumerate(sigs):
            yield sig, witnesses[i] if segwit else []
    if pos != len(raw):
        raise ValueError(f"block parse ended at byte {pos} of {len(raw)}")


def block_reveal_prefixes(raw: bytes, out: array, counts: BlockCounts) -> None:
    """Append the 8-byte prefixes of every hash a block's inputs reveal."""
    found: list[bytes] = []
    inputs = 0
    for script_sig, witness in block_inputs(raw):
        inputs += 1
        input_reveals(script_sig, witness, found)
    counts.inputs += inputs
    counts.reveals += len(found)
    out.extend(int.from_bytes(h[:8], "big") for h in found)


def match(candidates: Iterable[int] | array, targets: np.ndarray) -> np.ndarray:
    """The distinct candidates that appear in ``targets`` (sorted, unique, uint64)."""
    if isinstance(candidates, array):
        values = np.unique(np.frombuffer(candidates, dtype=np.uint64))
    else:
        values = np.unique(np.fromiter(candidates, dtype=np.uint64))
    if values.size == 0 or targets.size == 0:
        return values[:0]
    idx = np.searchsorted(targets, values)
    idx[idx == targets.size] = targets.size - 1
    return values[targets[idx] == values]
