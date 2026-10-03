## What the trace shows

This trace started from the 117 Bitcoin addresses that OFAC lists for Hydra Market
(SDN entry 36216) and followed every transaction that spent from them, using a
self-hosted Bitcoin Core node. Every figure below is a heuristic estimate.

### 1. Spending stops on the takedown date

Several of the busiest listed addresses (`3ES6pqCu…`, `34WWXwFK…` and `3MP7yBSG…`)
made their last spend on **2022-04-05**. That is the day German authorities seized
Hydra's servers and OFAC designated it
([Elliptic](https://www.elliptic.co/insights/5-billion-darknet-market-hydra-seized-by-german-authorities/)).
The trace found that date in the chain data without being told about it.

Some addresses kept spending afterwards, for example `1HH8eiua…` until 2022-04-08 and
`1H8sDTTg…` until 2022-06-03; the table of sanctioned addresses below lists every last
spend date. The authorities reported seizing 543.3 BTC in 88 transactions, so
some of these spends may be that seizure, or someone who still held keys. This data
cannot tell the two apart.

### 2. Hydra rotated its hot wallets

The three busiest addresses took turns:

- `3GXdtA6k…` from 2021-02-18 to **2021-08-05**
- `3K4rjdh8…` from **2021-08-05** to 2021-11-15
- `35KAdTa2…` from 2021-10-30 to 2021-12-19

One stops on the day the next starts. Each reused a single address for thousands of
payments and sent its change back to itself, which is why gross totals overstate them:
`3K4rjdh8…` spent 26,194 BTC gross but only 1,691 BTC net.

### 3. The listed addresses are many wallets, connected by money

The 117 addresses fall into 102 separate clusters: 100 hold one listed address, one
holds 3 and one holds 14. Common-input spending never joins them, but about 9,111 BTC
moved from one listed cluster to another. That fits OFAC's attribution of all of them
to one operation, and it is why the headline figure counts the clusters together.

### 4. The largest cluster is a range, but its outflow is not

The cluster holding 14 listed addresses has **286,549** addresses when batch sweeps
are included and **12,395** without them. Batch sweeps are how a custodial market
gathers its users' deposits, but also how unrelated wallets get merged by mistake, so
its size is uncertain. The value that left it barely moves between the two views
(about 35,246 to 36,064 BTC), so the value figures hold up even though the size does
not.

At the level of Hydra as a whole the range widens to 47,326–51,061 BTC, because about
2,800 BTC moved between Hydra's listed wallets into addresses linked to them only
through batch sweeps; the high end counts that as leaving.

### 5. Money did not leave through one exit

Of 669 sweeps that ended a branch, 133 moved funds within Hydra's own clusters
(215 BTC) and 536 went to outside destinations (1,585 BTC). The largest single outside
destination received only about 31 BTC.

### What this does not show

It does not identify anyone, and it does not show that any address outside OFAC's list
belongs to Hydra. It shows where coins moved and which addresses the heuristics group
together. Value figures are gross flows in BTC over the whole active period shown above,
not balances, profits or dollar amounts.
