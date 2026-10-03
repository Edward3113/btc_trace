"""Output script classification by how much of the key is on-chain."""

import pytest

from btc_trace.scripts import hash_only, key_in_output, script_type

KEY33 = "02" + "11" * 32
KEY65 = "04" + "22" * 64


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ("21" + KEY33 + "ac", "pubkey"),
        ("41" + KEY65 + "ac", "pubkey"),
        ("51" + "21" + KEY33 + "21" + KEY33 + "52ae", "multisig"),  # 1-of-2
        ("76a914" + "ab" * 20 + "88ac", "pubkeyhash"),
        ("a914" + "ab" * 20 + "87", "scripthash"),
        ("0014" + "ab" * 20, "witness_v0_keyhash"),
        ("0020" + "ab" * 32, "witness_v0_scripthash"),
        ("5120" + "ab" * 32, "witness_v1_taproot"),
        ("51024e73", "anchor"),
        ("6a0401020304", "nulldata"),
        ("5220" + "ab" * 32, "witness_unknown"),  # witness v2, e.g. a future P2MR output
        ("5221" + "ab" * 32, "nonstandard"),  # push length does not match
        ("0015" + "ab" * 21, "nonstandard"),
        ("deadbeef", "nonstandard"),
        ("76A914" + "AB" * 20 + "88AC", "pubkeyhash"),  # case does not matter
    ],
)
def test_script_types(script, kind):
    assert script_type(script) == kind


def test_exposure_classes():
    assert all(key_in_output(k) for k in ("pubkey", "multisig", "witness_v1_taproot"))
    assert not key_in_output("pubkeyhash")
    assert all(
        hash_only(k)
        for k in ("pubkeyhash", "scripthash", "witness_v0_keyhash", "witness_v0_scripthash")
    )
    assert not hash_only("witness_v1_taproot") and not hash_only("nulldata")
