# btc_trace

Heuristic Bitcoin fund tracing that starts from addresses on the U.S. Treasury's OFAC
sanctions list and follows funds through the blockchain, using a self-hosted full node.

> **Status:** early scaffold. The node client, OFAC address extraction, and first
> clustering heuristics work and are tested. Multi-hop tracing and the published report
> are next (see [Roadmap](#roadmap)).

## Why this project exists

Ransomware operators, sanctioned exchanges, and darknet markets move funds on a public
ledger. Blockchain intelligence analysts trace those funds by combining public data
(sanctions designations, the blockchain itself) with clustering heuristics that estimate
which addresses are controlled by the same entity. Commercial platforms do this at
scale, but the underlying techniques are well documented and can be reproduced with
public data and a full node.

This project rebuilds those techniques from first principles:

- **Starting points from public sanctions data.** OFAC's SDN list includes Bitcoin
  addresses tied to designated parties. `btc-trace ofac` extracts them along with the
  SDN entry each belongs to.
- **Clustering heuristics.** Common-input ownership (addresses spent together in one
  transaction likely share an owner) and conservative change detection (address reuse,
  script-type matching, round payment amounts).
- **Awareness of where heuristics fail.** Likely CoinJoin transactions are flagged and
  excluded instead of being clustered, because they deliberately break the
  common-input assumption.
- **Self-verified data.** Queries go to a Bitcoin Core node the author runs, not a
  third-party API, so every result traces back to independently validated chain data.

It is the third project in a series; the first is
[pqc-inventory](https://github.com/Edward3113/pqc-inventory), a post-quantum
cryptography inventory scanner.

## Ethics and scope

- Only public blockchain data and public sanctions data are used.
- Clustering output is a **heuristic estimate**, not proof of ownership, identity, or
  wrongdoing. Every report says so.
- The project does not attempt to identify private individuals.
- Sanctions designations change. Always use the current list from OFAC, and treat
  anything here as research, not compliance screening.

## Quick start (no node needed)

Requires [uv](https://docs.astral.sh/uv/). Everything below runs on bundled fixtures.

```bash
git clone https://github.com/Edward3113/btc_trace.git
cd btc_trace
uv sync
uv run pytest
uv run btc-trace ofac tests/fixtures/sdn_advanced_sample.xml
```

The sample XML is **synthetic**. It mimics the structure of OFAC's file but contains
only well-known example addresses, not sanctions data.

## Using real OFAC data

1. Download `SDN_ADVANCED.XML` from OFAC's
   [Sanctions List Service](https://sanctionslist.ofac.treas.gov/Home/SdnList).
2. Extract the Bitcoin (XBT) addresses:

   ```bash
   mkdir -p data
   uv run btc-trace ofac SDN_ADVANCED.XML --json > data/sanctioned_xbt.json
   ```

Downloaded lists and generated data are ignored by git.

## Connecting your own node

`btc-trace` talks to Bitcoin Core over JSON-RPC. Settings come from environment
variables so no address or credential is ever written to the repo:

| Variable | Meaning |
| --- | --- |
| `BTC_URL` | RPC URL, for example `https://<node>.local:<port>/` |
| `BTC_USER`, `BTC_PASS` | RPC credentials |
| `BTC_CA_CERT` | CA file to verify the node against (recommended for StartOS; see below). Otherwise the OS trust store is used |
| `BTC_FIXTURES` | Directory of saved replies; selects offline mode |
| `BTC_TIMEOUT` | Seconds to wait for one RPC reply (default 900; block scans are slow) |

Transaction lookups need `txindex=1`, and tracing needs the block filter index
(`blockfilterindex=1`). Check both with:

```bash
uv run btc-trace node
# or, without Python:
./scripts/node_check.sh
```

**StartOS note:** StartOS publishes each service interface on its own LAN port, which is
not Bitcoin Core's internal 8332. Use the address StartOS lists for the Bitcoin Core RPC
interface, and create credentials with the service's *Generate RPC User Credentials*
action.

On macOS, set `BTC_CA_CERT` to the StartOS root CA file. Without it, Python checks the
node's certificate with the macOS trust store, which rejects some certificate key types
and elliptic curves with "certificate is using a broken key size", even when the CA is
trusted. Verifying against the CA file uses OpenSSL, which accepts them.

### Recording fixtures

`--record DIR` saves every node reply as a JSON fixture that can later be replayed with
`BTC_FIXTURES=DIR`. Fixtures contain only public blockchain data, which is the same on
every node.

```bash
uv run btc-trace --record recordings tx <txid>
BTC_FIXTURES=recordings uv run btc-trace tx <txid>
```

## Commands

| Command | What it does |
| --- | --- |
| `btc-trace node` | Node version, chain state, index status |
| `btc-trace ofac FILE [--ticker XBT] [--json]` | Extract sanctioned addresses |
| `btc-trace tx TXID` | Decode a transaction, flag likely CoinJoin, guess change |
| `btc-trace trace ADDR... [--seeds FILE] [--depth N] [--min-btc X] [--start-height H] [--out FILE]` | Follow funds forward from seed addresses |

## How tracing works

Bitcoin Core does not index which transaction spent a given output, so the tracer works
one level at a time:

1. `scanblocks` searches the node's BIP158 block filters for every block that touches
   the current frontier addresses, starting at the height each address was reached.
   False positives are removed on the node.
2. Each matching block is fetched with `getblock <hash> 3`, which includes the output
   each input spends, and the transactions spending from a frontier address are kept.
3. Every output of those transactions is recorded as a hop, labelled *change
   (heuristic)* or *payment*. Outputs above `--min-btc` become the next frontier,
   until `--depth` or `--max-addresses` is reached.

Likely CoinJoins end the trace on that branch and are listed in the report, since their
outputs cannot be tied to specific inputs. Scanning the whole chain can take many
minutes, so start with a narrow `--start-height` and a shallow `--depth`.

## Roadmap

1. ~~Multi-hop tracing with depth and value limits.~~ Done.
2. A static HTML report published with GitHub Pages, viewable without installing anything.
3. Validation of report output against a JSON Schema.
4. Phase 2: quantum exposure analysis, measuring BTC held in outputs whose public keys
   are already visible on-chain.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI runs linting, tests, and CodeQL on every push. CI never contacts a node.

## License

To be decided. The only runtime dependency, truststore, is MIT-licensed.

## Acknowledgments

- [Bitcoin Core](https://bitcoincore.org/) (MIT), the node software queried over RPC.
- U.S. Treasury, Office of Foreign Assets Control, for the public
  [SDN list](https://ofac.treasury.gov/sanctions-list-service).
- [0xB10C/ofac-sanctioned-digital-currency-addresses](https://github.com/0xB10C/ofac-sanctioned-digital-currency-addresses)
  (MIT), whose approach to parsing the SDN Advanced XML this project follows.
- [Start9 / StartOS](https://start9.com/), which hosts the author's node.
- [truststore](https://github.com/sethmlarson/truststore) (MIT), for using the OS trust
  store for TLS.
- [uv](https://github.com/astral-sh/uv), [Ruff](https://github.com/astral-sh/ruff),
  [pytest](https://pytest.org/), and GitHub CodeQL.
- Developed with assistance from Claude (Anthropic).
