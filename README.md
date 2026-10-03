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
| `btc-trace show REPORT` | Print a saved trace report as readable hops with reasons |
| `btc-trace report REPORT [--findings FILE] [--mark DATE=LABEL] [--out FILE]` | Render a trace as one self-contained HTML page |
| `btc-trace scan-status` / `btc-trace scan-abort` | Check or stop a block scan on the node (it runs one at a time) |

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

Each hop carries a label, the reasons behind it, and any co-spent input addresses
from outside the trace (common-input evidence):

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
interrupted trace resumes where it stopped (`--no-cache` turns this off). The node runs one scan at a time; Ctrl+C or a
timeout stops the scan on the node as well.

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
**Settings → Pages** choose **Deploy from a branch**, branch `main`, folder `/docs`.
The page contains only public chain and sanctions data; no node address or credential
is ever written into a report.

## Roadmap

1. ~~Multi-hop tracing with depth and value limits.~~ Done.
2. ~~A static HTML report for GitHub Pages.~~ Done (`btc-trace report`).
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

[MIT](LICENSE). Dependencies were checked first: the only runtime dependency,
truststore, is MIT-licensed, as are the development tools pytest and Ruff.

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
