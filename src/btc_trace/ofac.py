"""Extract sanctioned digital currency addresses from OFAC's SDN Advanced XML.

Download ``SDN_ADVANCED.XML`` from OFAC's Sanctions List Service
(https://sanctionslist.ofac.treas.gov/Home/SdnList). In that file, each address is a
Feature on a DistinctParty whose FeatureTypeID points at a reference value such as
"Digital Currency Address - XBT". The parsing approach follows 0xB10C's
ofac-sanctioned-digital-currency-addresses (MIT). Namespaces are matched with
wildcards so a change to OFAC's namespace URI does not break parsing.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: S405 - parses a trusted government file
from dataclasses import dataclass
from pathlib import Path

FEATURE_PREFIX = "Digital Currency Address - "

# Mainnet formats: legacy base58 (1..., 3...) and bech32/bech32m (bc1...).
# This is a shape check only; it does not verify checksums.
_BTC_ADDRESS = re.compile(r"^(?:[13][1-9A-HJ-NP-Za-km-z]{25,34}|bc1[02-9ac-hj-np-z]{11,71})$")


@dataclass(frozen=True, order=True)
class SanctionedAddress:
    address: str
    sdn_ref: str  # the DistinctParty FixedRef, i.e. the SDN entry it belongs to
    sdn_name: str = ""  # the entry's primary name, e.g. "HYDRA MARKET"


@dataclass
class ExtractResult:
    ticker: str
    addresses: list[SanctionedAddress]
    rejected: list[str]  # entries that did not look like mainnet Bitcoin addresses


def is_bitcoin_address(text: str) -> bool:
    return bool(_BTC_ADDRESS.match(text))


def _local(tag: str) -> str:
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


def party_name(party: ET.Element) -> str:
    """Primary name of an SDN entry, falling back to its first recorded name."""
    aliases = [el for el in party.iter() if _local(el.tag) == "Alias"]
    ordered = [a for a in aliases if a.get("Primary") == "true"] + aliases
    for alias in ordered:
        parts = [
            (el.text or "").strip()
            for el in alias.iter()
            if _local(el.tag) == "NamePartValue" and (el.text or "").strip()
        ]
        if parts:
            return " ".join(parts)
    return ""


def extract_addresses(xml_path: str | Path, ticker: str = "XBT") -> ExtractResult:
    root = ET.parse(xml_path).getroot()  # noqa: S314

    label = FEATURE_PREFIX + ticker
    type_ids = {
        el.get("ID")
        for el in root.iterfind(".//{*}ReferenceValueSets/{*}FeatureTypeValues/*")
        if (el.text or "").strip() == label
    }
    if not type_ids:
        raise ValueError(f"feature type {label!r} not found; is this SDN_ADVANCED.XML?")

    found: set[SanctionedAddress] = set()
    rejected: set[str] = set()
    for party in root.iterfind(".//{*}DistinctParties/{*}DistinctParty"):
        sdn_ref = party.get("FixedRef", "")
        name = party_name(party)
        for feature in party.iter():
            if feature.get("FeatureTypeID") not in type_ids:
                continue
            for detail in feature.iterfind(".//{*}VersionDetail"):
                value = (detail.text or "").strip()
                if not value:
                    continue
                if ticker == "XBT" and not is_bitcoin_address(value):
                    rejected.add(value)
                    continue
                found.add(SanctionedAddress(value, sdn_ref, name))

    return ExtractResult(ticker=ticker, addresses=sorted(found), rejected=sorted(rejected))
