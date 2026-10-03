from pathlib import Path

import pytest

from btc_trace.ofac import extract_addresses, is_bitcoin_address

SAMPLE = Path(__file__).parent / "fixtures" / "sdn_advanced_sample.xml"


def test_extracts_xbt_addresses_with_sdn_refs():
    result = extract_addresses(SAMPLE)
    pairs = {(a.address, a.sdn_ref) for a in result.addresses}
    assert pairs == {
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "99001"),
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "99003"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "99001"),
        ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0", "99002"),
    }


def test_ignores_other_feature_types():
    addresses = {a.address for a in extract_addresses(SAMPLE).addresses}
    assert "0x0000000000000000000000000000000000000000" not in addresses
    assert "example.invalid" not in addresses


def test_rejects_malformed_entries():
    assert extract_addresses(SAMPLE).rejected == ["not-a-bitcoin-address"]


def test_other_ticker():
    result = extract_addresses(SAMPLE, ticker="ETH")
    assert [a.address for a in result.addresses] == ["0x0000000000000000000000000000000000000000"]


def test_unknown_ticker_raises():
    with pytest.raises(ValueError, match="not found"):
        extract_addresses(SAMPLE, ticker="NOPE")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", True),
        ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", True),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", True),
        ("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", False),  # testnet
        ("0x0000000000000000000000000000000000000000", False),
        ("", False),
    ],
)
def test_address_shape(text, expected):
    assert is_bitcoin_address(text) is expected
