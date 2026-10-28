# Paper-only Solana memecoin desks

> **PAPER TRADING ONLY. No real money, no wallets, no keys.** GitHub Actions runs two simulated $500 desks about once a minute using public read-only APIs (DexScreener, GeckoTerminal, RugCheck). Nothing here buys or sells anything.

Strict thresholds come from an X post by @x_insider4. Treat them as an experiment. The LOOSE desk is a looser comparison run.

## Commands
    python3 desk.py cycle [--desk strict|loose]       # one shared scout pass + both desks (or one); ~5-45 s
    python3 desk.py recap [--desk strict|loose]       # last-24h report, one section per desk
    python3 desk.py calibrate [--desk strict|loose]   # judge calibration + re-priced passes
    python3 alerts.py [--peek]                        # new BUY/SELL lines per desk since last run (box-local cursors)

## SCOUT (shared, deduped by mint, ~150 Solana mints per cycle)
DexScreener: token-profiles/latest, token-boosts/latest, token-boosts/top, community-takeovers/latest, ads/latest, and search
(pumpswap, raydium, pump, meteora, bonk, moonshot, launchlab, "solana meme"; pairs < 3 days old). GeckoTerminal: solana new_pools
pages 1-2 and trending_pools (1h): 3 calls/min against a ~30/min limit. Pair data is batched 30 mints per DexScreener call.
RugCheck reports are cached 30 min per mint in `scout_cache.json` (runner-local, gitignored), at most 40 fresh calls per cycle,
and RugCheck is skipped when DexScreener data already fails a desk. Rejected coins are re-scored after 15 min.
Open positions are re-priced every cycle before scouting.

## Desk profiles (`PROFILES` in desk.py)
| rule | STRICT | LOOSE |
|---|---|---|
| mint authority revoked | required | required |
| freeze authority revoked | (danger flag only) | required |
| LP locked/burned | >= 95% | >= 80% |
| top-10 holders | <= 20% | <= 35% |
| 1h volume | >= $50k | >= $10k |
| liquidity | - | >= $10k |
| dev flag | rugged or danger dev/insider flag kills | only RugCheck "rugged" kills |
| missing data | fail | LP / top-10 / dev missing = neutral |
| ANALYST floor | 0.60 | 0.45 |
| JUDGE gate | buy p>=0.70, organic>=0.65, not late, exit risk<=3 | buy p>=0.55, organic>=0.50, not late, exit risk<=4 |
| size | 8% of equity | 5% of equity, max 6 open |
| hard stop | -6% | -8% |
| trailing | arms +12%, 10% giveback | arms +10%, 10% giveback |
| time stop | - | 6h if under +3% |

Both: paper fills at the live DexScreener price with a 2% haircut each side, no re-entry into a traded token, no new entries in a cycle that booked a loss.

## Files
STRICT: `state.json`, `log.jsonl`. LOOSE: `state_loose.json`, `log_loose.jsonl` (bank started 2026-10-28). `loop.log` (runner output incl. cycle seconds),
`terminal.html` (both desks). Unchanged reject reasons are not re-logged. Reset a desk: delete its state and log files.
