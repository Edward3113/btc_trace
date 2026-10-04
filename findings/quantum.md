## What the measurement shows

This page measures every unspent output in Bitcoin's UTXO set at block 969,756
(3 October 2026). The data comes from a self-hosted Bitcoin Core node. The snapshot was
verified coin for coin against the node's own UTXO set hash, and every one of the
3,515,834,269 inputs in the chain was read to find address reuse.

### 1. About 35% of all bitcoin sits behind a visible public key

**7,118,300 BTC**, 35.4% of the 20,092,760 BTC in existence, is held in outputs whose
public key is already on-chain. A large enough quantum computer could, in principle,
derive those private keys at leisure. That is close to the published estimates listed
below: BIP-361's "over 34%" and Google Quantum AI's 6.9 million BTC. This measurement
was made independently from raw chain data on a home node.

### 2. Most exposure comes from address reuse, not from the output type

Only **1.93 million BTC (27%)** is exposed because its output type shows a key: P2PK,
bare multisig or Taproot. The other **5.18 million BTC (73%)** sits in hash-based
outputs (P2PKH, P2WPKH, P2SH, P2WSH) at addresses that have already spent once, which
put their key or script on-chain. Owners can fix this today by moving the coins to a
fresh address. Reuse is highest for P2WSH (45% of its value), the type large multisig
and custodial wallets favor.

### 3. The hard part is coins that will not move

**2.71 million BTC** with a visible key has not moved in five years or more. Most of it
is **1.70 million BTC** of early-mining P2PK coins from Bitcoin's first years, in 34,151
outputs. Their owners would have to move them to protect them, and many of those keys
are probably lost. These are the coins at the center of BIP-361's proposal to restrict
spending of exposed coins after a migration period.

### 4. Taproot is growing but holds little value

Taproot outputs hold 219,905 BTC, up from Chaincode Labs' 146,715 BTC in May 2025. They
are a third of all outputs by count but about 1% by value, because most are small.
Taproot shows a key by design, which is what BIP-360's proposed P2MR output type would
change.

### 5. Sanctioned coins are mostly protected

Most coins still held by OFAC-listed addresses sit behind fresh, never-reused SegWit
addresses, so their keys are not visible. Most of the exposed balance comes from one
2019 designation's reused legacy addresses. A balance on a sanctioned address does not
show who controls it.

## What this does not show

- **Who holds the coins.** One output is not one person. Exchanges hold coins for
  millions of customers, and one wallet can hold thousands of outputs.
- **When a quantum computer arrives.** None today can run Shor's algorithm at this
  scale. This measures exposure, not imminent risk.
- **Keys exposed off-chain.** Extended public keys shared with services, and similar
  leaks, are invisible to a blockchain scan, so the true exposure is somewhat higher.
