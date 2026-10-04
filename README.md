# btc_trace

![CI](https://github.com/Edward3113/btc_trace/actions/workflows/ci.yml/badge.svg)

**btc_trace** follows Bitcoin from addresses on the U.S. Treasury's OFAC sanctions list. It
uses only public blockchain data, read from your own Bitcoin Core node. It groups addresses
that are probably controlled by the same wallet, labels likely change outputs and gives the
reason for each label, and stops tracing at CoinJoins and exchange-style sweeps, where
following the money further would just be guessing. Each report gives a low and a high
estimate of how much left the sanctioned wallets, is checked against a published JSON
Schema, and can be turned into a self-contained HTML report. Every result is a heuristic
estimate, not proof of who owns an address.

The included case study traces Hydra Market's listed addresses: an estimated
47,326–51,061 BTC left Hydra's wallets, and spending stopped on 5 April 2022, the day it
was taken down and sanctioned.

**[View the Hydra Market report](https://edward3113.github.io/btc_trace/)** and the
[written findings](findings/hydra.md) behind it.

Phase 2 measures how much bitcoin is exposed to a future quantum computer. At block
969,756, **7,118,300 BTC (35.4% of all bitcoin)** sat behind a public key already visible
on-chain, three quarters of it because of address reuse. **[View the quantum exposure
report](https://edward3113.github.io/btc_trace/quantum/)** and its
[findings](findings/quantum.md).

> **Status:** both phases are done: sanctions tracing with its Hydra Market report, and
> quantum-exposure analysis of the sanctioned addresses and of the whole UTXO set,
> including address reuse across the whole chain, with its own report page.

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

## Companion project

btc_trace is the third project in a series. The first,
[pqc-inventory](https://github.com/Edward3113/pqc-inventory), finds quantum-vulnerable
cryptography on TLS and SSH endpoints. Phase 2 of this project (see [Roadmap](#roadmap))
asks the same question of the Bitcoin blockchain: how much BTC sits in outputs whose
public keys are already visible on-chain. The two tools share no code, and each works on
its own.

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
| `BTC_SCAN_CHUNK` | Blocks per `scanblocks` call (default 25000). Lower it if a scan with many addresses times out |
| `BTC_WORKERS` | Blocks fetched in parallel (default 4; same as `--workers`) |

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
| `btc-trace trace ADDR... [--seeds FILE] [--sdn REF] [--depth N] [--min-btc X] [--start-height H] [--out FILE]` | Follow funds forward from seed addresses |
| `btc-trace exposure ADDR... [--seeds FILE] [--sdn REF] [--out FILE]` | How much of what the addresses hold today has a visible public key |
| `btc-trace dump-utxos [--name FILE]` | Have the node write its UTXO set to a file (the only command that writes anything on the node) |
| `btc-trace utxo-stats SNAPSHOT [--revealed FILE] [--out FILE]` | Measure a UTXO snapshot by script type and age (and reuse, with a reveal scan), and verify it against the node's own hash |
| `btc-trace reveal-scan SNAPSHOT [--workers N]` | Read every block to find hash-based addresses whose key or script has been revealed |
| `btc-trace show REPORT` | Print a saved trace report as readable hops with reasons, or an exposure summary |
| `btc-trace validate REPORT` | Check a saved report against its JSON Schema |
| `btc-trace report REPORT [--findings FILE] [--mark DATE=LABEL] [--exposure FILE] [--link LABEL=URL] [--out FILE]` | Render a trace or UTXO set report as one self-contained HTML page |
| `btc-trace scan-status` / `btc-trace scan-abort` | Check or stop a block or UTXO scan on the node (each runs one at a time) |

## How tracing works

Bitcoin Core does not index which transaction spent a given output, so the tracer works
one level at a time:

1. `scanblocks` searches the node's BIP158 block filters for every block that touches
   the current frontier addresses, starting at the height each address was reached.
   The filters occasionally match a block that does not involve the addresses; those
   are left for step 2 to discard, so the node reads each block from disk only once.
2. Each matching block is fetched with `getblock <hash> 3`, which includes the output
   each input spends, several at a time (`--workers`), and the transactions spending
   from a frontier address are kept.
3. Every output of those transactions is recorded as a hop. Outputs above `--min-btc`
   become the next frontier, until `--depth` or `--max-addresses` is reached.

Each hop carries a label and the reasons behind it. Input addresses spent alongside a
traced address (common-input evidence) are listed once per transaction in the report's
transactions table:

| Label | Meaning |
| --- | --- |
| *change (heuristic)* | Probably returned to the spender (address reuse, matching script type, or the other output is a round amount) |
| *payment (heuristic)* | The other output was identified as change, so this one probably left the spender's control |
| *undetermined* | No reliable signal: a single output (payment or self-transfer), more than two outputs, or conflicting signals |

Two kinds of transaction end a branch rather than being followed:

- **Likely CoinJoins** (several equal-value outputs and inputs), whose outputs cannot be
  tied to specific inputs.
- **Sweeps**: at least `--service-min-inputs` (default 20) input addresses from outside
  the trace gathered into one or two outputs. A sweep whose destination is in the
  spender's own cluster is an *internal consolidation* (the funds stayed with the same
  owner); otherwise it is a *service consolidation*, the usual sign that funds entered
  an exchange or other custodial service. Either way the trace stops there, because
  past that point it would follow pooled funds.

The report also lists **clusters**: addresses linked by common-input spending or by
change outputs, with the transactions that link them. Large custodial wallets sweep
hundreds of deposit addresses while paying several recipients at once. These **batch
sweeps** are kept in clustering, since they are usually the wallet's own deposits, but
they are also how unrelated wallets get merged by mistake. So each cluster reports how
many of its links are batch sweeps and how big the seed's part would be without them.
A cluster that shrinks dramatically without batch sweeps deserves a closer look before
it is attributed to anyone.

**Value leaving a cluster** answers "how much left the sanctioned entity?" better than
per-address totals. Every spend is credited to the spender's cluster, and only outputs
to addresses outside that cluster count, so change sent to the owner's own new
addresses drops out. It is given as a range: the low figure treats the whole cluster as
one owner, and the high figure uses only the core without batch sweeps. Value swept
into services and into CoinJoins is shown separately, and so is the part that reached
another seed's cluster: that money may leave again from there and be counted twice in
the totals.

To avoid that double count, each report also measures **value leaving an SDN entry as a
whole**: every cluster holding one of the entry's seeds counts as inside, so transfers
between them drop out. This is the figure to quote for "how much left the sanctioned
entity", with a month-by-month breakdown for charts.

Each report also has:

- a **per-seed summary**: for every seed, its cluster, how many transactions spent from
  it, its net outflow (spent minus change returned to the same address), and the first
  and last spend dates;
- **dates** (UTC, from block times) on every hop, stop and transaction;
- a **transactions** table, so co-spent inputs are stored once per transaction rather
  than repeated on every hop.

`btc-trace show REPORT` prints the summaries first, then individual hops for small
reports (or `--hops N` for large ones).

### Tracing every address of one SDN entry

`btc-trace ofac --json` records each address's SDN entry (`sdn_ref`) and name. `--sdn`
selects one entry's addresses as seeds, and the report names the entry:

```bash
uv run btc-trace trace --seeds data/sanctioned_xbt.json --sdn <sdn_ref> --depth 1 --out reports/entry.json
```

Likely CoinJoins end the trace on that branch and are listed in the report, since their
outputs cannot be tied to specific inputs. Scanning the whole chain can take many
minutes, so start with a narrow `--start-height` and a shallow `--depth`. Scans run in
chunks, with a progress bar showing the block reached and the blocks fetched.
Dropped connections are retried, and progress is saved in `.btc_trace_cache/` so an
interrupted trace resumes where it stopped (`--no-cache` turns this off). The node runs
one scan at a time; Ctrl+C or a timeout stops the scan on the node as well.

## Quantum exposure

A quantum computer large enough to run Shor's algorithm could derive a private key
from its public key. What matters for a coin is therefore whether its public key is
already visible on the blockchain, which BIP-360 calls *long exposure*:

| Output type | Public key on-chain? |
| --- | --- |
| P2PK, bare multisig | Yes: the output script contains the key |
| P2TR (Taproot) | Yes: the output is a tweaked public key, spendable by key path |
| P2PKH, P2SH, P2WPKH, P2WSH | Only a hash, until the address spends; after that, any coins it still holds or receives are exposed |

`btc-trace exposure` measures this for a list of addresses, such as the OFAC list:

```bash
uv run btc-trace exposure --seeds data/sanctioned_xbt.json --out reports/exposure_ofac.json
uv run btc-trace show reports/exposure_ofac.json
```

1. `validateaddress` gives each address's output script, and so its type.
2. One `scantxoutset` pass over the node's UTXO set finds everything the addresses
   hold today, with the block height the snapshot was taken at.
3. For hash-based addresses that still hold coins, the block filter index finds
   candidate blocks and each is read, oldest first, until the address's first spend
   is found or the candidates run out. Reading stops as soon as every address has been
   decided, and progress is saved so an interrupted run resumes.

Each address is reported as *key in output*, *spent before* (with the first spend's
block, date and txid), *hash only* or *no balance*, with totals by script type and by
SDN entry. Exposure describes keys, not owners: it does not say who controls an
address, and sanctioned funds may already be frozen or seized off-chain. A key revealed
by one address type also exposes the same key's other address types (a P2PKH spend
reveals the key behind the matching P2WPKH address); this check covers only the
addresses listed.

### The whole UTXO set

`btc-trace exposure` looks at chosen addresses. To measure every coin, the node writes
its whole UTXO set to a file with `dumptxoutset`, and `btc-trace utxo-stats` reads it:

```bash
uv run btc-trace dump-utxos          # node writes utxo-HEIGHT.dat (about 10 GB, several minutes)
# copy the file from the node into data/ over SSH, next to the saved data/utxo-HEIGHT.json
uv run btc-trace utxo-stats data/utxo-HEIGHT.dat --out reports/utxo-HEIGHT.json
```

`utxo-stats` streams the file once (a few minutes for the roughly 170 million coins on
mainnet) and adds up coins and BTC by script type and by the 1,000-block range each
coin was created in, which is when it last moved. Coins created five or more years
before the snapshot count as dormant. The file is decoded as Bitcoin Core's own
`contrib/utxo-tools/utxo_to_sqlite.py` does.

While reading, it recomputes the UTXO set hash (`hash_serialized_3`, a SHA256d over
every coin) and compares it, along with the coin count and base block, with what the
node reported when it wrote the file. A match proves every coin was copied and decoded
exactly; a mismatch makes the command fail. With a node configured it also looks up the
date of each 1,000-block range.

On its own this counts types whose *output* shows a key (P2PK, bare multisig,
Taproot). For comparison, [Chaincode Labs](https://chaincode.com/bitcoin-post-quantum.pdf)
counted about 1,720,747 BTC in P2PK and 146,715 BTC in P2TR outputs in May 2025.

### Address reuse across the whole chain

Coins at a hash-based address are exposed too once that address has spent: the spend
put its public key (P2PKH, P2WPKH) or script (P2SH, P2WSH) on-chain. Bitcoin Core keeps
no index of this, so `btc-trace reveal-scan` reads every block once:

```bash
caffeinate -i uv run btc-trace reveal-scan data/utxo-HEIGHT.dat     # many hours; resumable
uv run btc-trace utxo-stats data/utxo-HEIGHT.dat --revealed data/utxo-HEIGHT.revealed.npy \
  --out reports/utxo-HEIGHT.json
```

1. It collects the address hash of every hash-based coin in the snapshot (the targets).
2. It reads blocks 0 to the snapshot height from the node as raw bytes, several worker
   processes at a time over kept-alive connections. About 700 GB of blocks cross the
   network as hex, so this takes hours; each finished range of 1,000 blocks is saved, and
   an interrupted scan resumes where it stopped.
3. For every input it hashes what the spend revealed back to the addresses it unlocks.
   It needs no record of which output each input spent:
   - a public key gives its P2PKH/P2WPKH hash and its P2SH-wrapped P2WPKH hash, and an
     uncompressed key also gives the compressed form's (the same key);
   - a P2SH redeem script gives its script hash, a P2WSH witness script its SHA-256
     (and its P2SH-wrapped hash), and keys inside those scripts count as revealed too;
   - Taproot spends and P2PK spends are skipped: those outputs show a key anyway.
4. Revealed hashes that match a target are kept, compared by their first 8 bytes. With
   tens of millions of targets and billions of comparisons, the expected number of
   false matches is far below one.

`utxo-stats --revealed` then adds, per type and per age bin, the BTC at addresses whose
key or script is already on-chain, and the total with a visible key either way. Published
estimates for that total are 6.26 million BTC (Project Eleven, cited by Chaincode, 2025)
to 6.9 million (Google Quantum AI, 2026).

Limits: a compressed key revealed by a spend does not mark the *uncompressed* P2PKH
address of the same key (that needs an elliptic-curve square root per key; such outputs
are rare and old). A revealed script with no keys in it (a hash lock, say) counts as
revealed although no key is exposed. Keys disclosed off-chain, such as shared extended
public keys, are invisible here.

## Report format

Every report follows a published JSON Schema (Draft 2020-12) and carries a
`report_version`: trace reports follow
[`trace-report.schema.json`](src/btc_trace/schemas/trace-report.schema.json), and
exposure reports, marked `"report_kind": "exposure"`, follow
[`exposure-report.schema.json`](src/btc_trace/schemas/exposure-report.schema.json), and
UTXO set reports, marked `"report_kind": "utxo-set"`, follow
[`utxo-set-report.schema.json`](src/btc_trace/schemas/utxo-set-report.schema.json). The schema documents each field, its
type and its allowed values, so anyone reading a report knows exactly what it contains.
The test suite checks that every kind of report the tool produces matches its schema,
so the code and the formats cannot drift apart unnoticed.

```bash
uv run btc-trace validate reports/hydra_d1.json
```

`btc-trace report` runs the same check before rendering and refuses a report that does
not match, such as one made by an older version. On a large report the check takes
about a minute; `--no-validate` skips it for a report you have already validated.

## Publishing a report page

`btc-trace report` turns a saved trace into one self-contained HTML page: headline
figures, your findings, a monthly chart of value leaving the entity, a timeline of when
the busiest addresses were active, and tables for every seed and cluster. The page loads
nothing from the internet, works in light and dark mode, and fits a phone screen.

```bash
uv run btc-trace report reports/hydra_d1.json \
  --findings findings/hydra.md \
  --mark "2022-04-05=Takedown and OFAC designation" \
  --out docs/index.html
```

The findings file is plain Markdown you write: the tool generates the facts, and the
interpretation stays yours. Raw HTML in it is shown as text, and links must be http(s).

To publish it with GitHub Pages, commit `docs/index.html`, then in the repository's
**Settings → Pages** set **Source** to **GitHub Actions**. The `Pages` workflow
(`.github/workflows/pages.yml`) deploys `docs/` whenever it changes, using actions that
run on Node 24.

The page contains only public chain and sanctions data; no node address or credential
is ever written into a report.

### The quantum exposure page

The same command renders a UTXO set report, written by `utxo-stats --revealed`, as a
second page at `docs/quantum/index.html`. It has the headline exposure figures, the
split by output type, a chart of when exposed coins last moved, a comparison with
published estimates, and the BIP-360 and BIP-361 context. `--exposure` adds the
sanctioned-address results from `btc-trace exposure`:

```bash
uv run btc-trace report reports/utxo-969756.json \
  --exposure reports/exposure_ofac.json --findings findings/quantum.md
```

`--link LABEL=URL` (repeatable) puts links to related pages above a page's title. The
quantum page links back to the Hydra report by default; the Hydra page links forward
with:

```bash
uv run btc-trace report reports/hydra_d1.json --findings findings/hydra.md \
  --mark "2022-04-05=Takedown and OFAC designation" \
  --link "Quantum exposure report →=quantum/"
```

The page shows totals by type and by year only, and never lists exposed addresses: a
ranked list of other people's exposed balances would be a target list. The one exception
is the sanctions section, which names OFAC entries (public designations), not addresses.

## Roadmap

1. ~~Multi-hop tracing with depth and value limits.~~ Done.
2. ~~A static HTML report for GitHub Pages.~~ Done (`btc-trace report`).
3. ~~Validation of report output against a JSON Schema.~~ Done (`btc-trace validate`).
4. Phase 2: quantum exposure analysis, measuring BTC held in outputs whose public keys
   are already visible on-chain.
   1. ~~Exposure of a list of addresses, such as the OFAC list.~~ Done
      (`btc-trace exposure`).
   2. ~~The whole UTXO set by script type and age, from a `dumptxoutset` snapshot.~~
      Done (`btc-trace dump-utxos`, `btc-trace utxo-stats`).
   3. ~~Address reuse across the whole chain: keys revealed by any earlier spend.~~
      Done (`btc-trace reveal-scan`, `btc-trace utxo-stats --revealed`).
   4. ~~Dormancy, and a report page framed against BIP-360 and BIP-361.~~ Done
      (`btc-trace report` on a UTXO set report).

## Freeing disk space

The UTXO snapshot (about 10 GB), the address lists built from it, and the progress
files that let scans resume are only needed while you work. `scripts/cleanup_data.sh`
lists them with their sizes and deletes them only when asked:

```bash
scripts/cleanup_data.sh            # dry run: what would be freed
scripts/cleanup_data.sh --yes      # delete it
scripts/cleanup_data.sh --all      # also include the reveal-scan results
```

It never touches `reports/`, `docs/`, `findings/`, the OFAC address file, or the small
`data/utxo-*.json` dump details, and it refuses to run outside this project.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI runs linting, tests, and CodeQL on every push. CI never contacts a node.

## Acknowledgments

- [Bitcoin Core](https://bitcoincore.org/) (MIT), the node software queried over RPC.
- U.S. Treasury, Office of Foreign Assets Control, for the public
  [SDN list](https://ofac.treasury.gov/sanctions-list-service).
- [0xB10C/ofac-sanctioned-digital-currency-addresses](https://github.com/0xB10C/ofac-sanctioned-digital-currency-addresses)
  (MIT), whose approach to parsing the SDN Advanced XML this project follows.
- [Start9 / StartOS](https://start9.com/), which hosts the author's node.
- [BIP-360](https://github.com/bitcoin/bips/blob/master/bip-0360.mediawiki) (Hunter
  Beast, Ethan Heilman, Isabel Foxen Duke), whose long-exposure classification of
  output types the exposure check follows.
- Bitcoin Core's [`utxo_to_sqlite.py`](https://github.com/bitcoin/bitcoin/tree/master/contrib/utxo-tools)
  (MIT), whose decoding of `dumptxoutset` files `utxo-stats` follows.
- Chaincode Labs, [*Bitcoin and Quantum Computing*](https://chaincode.com/bitcoin-post-quantum.pdf)
  (Milton and Shikhelman, May 2025), for the published P2PK and P2TR figures used as a
  cross-check.
- [truststore](https://github.com/sethmlarson/truststore) (MIT), for using the OS trust
  store for TLS.
- [jsonschema](https://github.com/python-jsonschema/jsonschema) (MIT), for checking
  reports against the report schema.
- [NumPy](https://numpy.org/) (BSD-3-Clause), for matching tens of millions of address
  hashes during the reveal scan.
- [uv](https://github.com/astral-sh/uv), [Ruff](https://github.com/astral-sh/ruff),
  [pytest](https://pytest.org/), and GitHub CodeQL.
- Developed with assistance from Claude (Anthropic).

## License

[MIT](LICENSE). Dependencies were checked first. Runtime: truststore (MIT),
jsonschema (MIT), whose own dependencies are MIT-licensed apart from typing_extensions
(PSF-2.0), and NumPy (BSD-3-Clause, with bundled parts under 0BSD, MIT, Zlib and
CC0-1.0); all are compatible. Development: pytest and Ruff (both MIT).
