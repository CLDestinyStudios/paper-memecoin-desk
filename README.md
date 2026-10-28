# Paper-only Solana memecoin desks

> **PAPER TRADING ONLY. No real money, no wallets, no keys.** GitHub Actions runs three simulated $500 desks about once a minute using public read-only APIs (DexScreener, GeckoTerminal, RugCheck, pump.fun frontend API, public Solana RPC). Nothing here buys or sells anything.

Strict thresholds come from an X post by @x_insider4. Treat them as an experiment. The LOOSE desk is a looser comparison run. PUMP (`pump.py`) paper-trades pump.fun coins still on their bonding curve.

## Commands
    python3 desk.py cycle [--desk strict|loose|pump]  # STRICT+LOOSE scout pass with PUMP in a parallel thread (or one desk); ~5-45 s
    python3 desk.py recap [--desk strict|loose|pump]  # last-24h report, one section per desk, then the scoreboard
    python3 desk.py calibrate [--desk strict|loose]   # judge calibration + re-priced passes (PUMP has no judge)
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

## PUMP desk (`pump.py`, pump.fun bonding-curve momentum, own $500 bank)
Paper only: no wallet, no key, no signing, no paid PumpPortal trade feed. Data, all free and keyless:
- discovery: `frontend-api-v3.pump.fun/coins` newest first, paged until it reaches coins already on the watchlist (up to 6 pages of 50).
  Fallback if that API is blocked: PumpPortal's free websocket `subscribeNewToken` for ~12 s per cycle.
- curve: one batched public-RPC `getMultipleAccounts` (100 curves per call) for every coin aged 2-10 min plus open positions:
  exact reserves, price, progress to graduation, `is_mayhem_mode` flag.
- 5-minute buys/sells: DexScreener `tokens/v1` (pump.fun curve pairs), 30 mints per call.
- holders: public-RPC `getProgramAccounts` on the mint (wallets holding it, curve excluded). This stands in for "unique buyers";
  pump.fun's own trade list API is not usable without a key.
- bundled launch: `getSignaturesForAddress` on the curve (extra buys in the creation block). Cached per coin.
- creator: `/coins?creator=<wallet>` (launches in the last 24 h) and the RugCheck summary ("Creator history of rugged tokens"), at most 6 fresh RugCheck calls per cycle.
Heavy checks run on at most 15 coins per cycle that already pass the curve and buy/sell filters. Runs within a 35 s budget.

**PUMP rules loosened Oct 28 2:4x AM PT** (caelan wanted it to trade). Old -> new: buyers 25 -> 10, buys:sells 2:1 -> 1.3:1,
curve band 15-60% -> 5-75%, creator <=10% -> <=15%, top-10 <=30% -> <=45%, top-5 majority 50% -> 80% of tokens outside the curve,
bundled buys in the creation block 0 -> up to 2, creator launches/24 h <=3 -> <=5, deep checks 8 -> 15 coins/cycle, RugCheck 3 -> 6 calls/cycle.
Max open 4 -> 6. Busy coins (>4000 curve txs) count the launch block as unknown instead of rejecting. Unchanged: mayhem skip, rug-history/rugged skip, 3% size, all exits (-15% curve stop, half at +50%, out at 90% curve, 45 min time stop, 5 min stall) — open positions are re-priced and exit-checked first thing every cycle.

| rule | PUMP |
|---|---|
| when | each coin checked every cycle while 2-10 min old |
| unique buyers | >= 10 (wallets holding, curve excluded) |
| flow | DexScreener 5 m buys >= 1.3x sells |
| curve | 5-75% of the way to graduation |
| creator | holds <= 15%; not RugCheck-flagged for past rugs / rugged; <= 5 launches in 24 h |
| holders | top 10 (curve excluded) <= 45% of supply; skip if the top 5 hold > 80% of the tokens outside the curve |
| launch | skip if more than 2 other buys landed in the creation block |
| skip | `is_mayhem_mode` coins, non-SOL pairs, graduated coins, non-1B supply |
| size | 3% of equity, max 6 open, no re-entry |
| fills | curve formula + 1.25% fee + 4% slippage penalty + 0.0005 SOL, each side |
| stop | -15% on the curve price |
| take profit | half at +50% on the curve price; the rest at 90% of the way to graduation (or on PumpSwap if it graduates first) |
| time | close at 45 min, or after 5 min held when the last 5 min had no buys |

Stops and targets are measured on the curve price. P&L includes all fees: a fresh position is marked about 11-12% under cost,
because selling back immediately would pay both sides' fees and slippage.

## Files
STRICT: `state.json`, `log.jsonl`. LOOSE: `state_loose.json`, `log_loose.jsonl` (bank started 2026-10-28). PUMP: `state_pump.json`
(bank, positions, watchlist size and the 10 in-window coins furthest up the curve), `log_pump.jsonl` (entries, exits incl. partial sells,
deep-check rejects, per-cycle counts), `pump_cache.json` (runner-local, gitignored: the rolling watchlist of coins under ~11 min old plus caches). `loop.log` (runner output incl. cycle seconds), `terminal.html` (all desks). Unchanged reject reasons are not re-logged. Reset a desk: delete its state and log files.
