# Paper-only Solana memecoin desk

> **PAPER TRADING ONLY. No real money, no wallets, no keys.** This repo runs a simulated $500 desk on GitHub Actions every ~5 minutes using public read-only APIs (DexScreener, RugCheck). Nothing here buys or sells anything.


**No real money.** No wallets, keys, signing, exchanges, or purchases. Only read-only public APIs:
DexScreener (pairs, price, liquidity, volume, txns) and RugCheck `/v1/tokens/<mint>/report` (mint/freeze authority, LP lock, top holders, risk flags). Neither needs a key.
Thresholds come from an X post by @x_insider4. Treat them as an experiment.

## Commands
    python3 /workspace/memecoin-desk/desk.py cycle      # one scan+manage pass (~10-60 s), safe to re-run
    python3 /workspace/memecoin-desk/desk.py recap      # morning report (last 24h)
    python3 /workspace/memecoin-desk/desk.py calibrate  # weekly judge calibration + re-priced passes

## Seats (all in desk.py; hard handoffs)
1. SCOUT: Solana tokens from DexScreener latest profiles/boosts, plus search (pumpswap/raydium/pump, pairs <3 days old). Up to 60 new candidates per cycle.
2. RISK: kills a token if mint authority is not revoked, LP is <95% locked/burned, top-10 holders exceed 20% (AMM, locker and pool accounts excluded), 1h volume is under $50k, or it has a rugged/danger dev/insider flag. One fail kills it, and missing data counts as a fail.
3. ANALYST: a 0-1 score from buy pressure, txn count, liquidity, turnover sanity, non-parabolic move, holder spread and socials. Under 0.6 dies.
4. JUDGE: a facts-only dossier (gaps are written as 'missing'). It answers organic, late, exit_risk 1-5 and buy/watch/pass with deterministic rules. The gate requires buy p>=0.70, organic=yes p>=0.65, late=no and exit_risk<=3. A watch is rechecked once on the next cycle; a second watch becomes a pass.
5. EXECUTOR: paper fill at the DexScreener price +2%. Size is fixed at 8% of current paper equity, and confidence never changes size.
6. RISK/EXIT: the hard stop is -6% after the 2% exit haircut. A trailing stop (10% off the peak, my choice) arms only after the price runs +12%. There is no averaging down and no re-entry into a token already traded. No new entries are allowed in a cycle that booked a loss.
7. LOGGER: `log.jsonl` records every entry, exit, watch, rejection (with reason) and cycle summary.

## Files
`state.json` (bank starts at $500; holds open positions, the watchlist and seen tokens), `log.jsonl`, `terminal.html` (regenerated each cycle).
Reset: delete state.json and log.jsonl.
