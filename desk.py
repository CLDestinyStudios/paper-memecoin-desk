#!/usr/bin/env python3
"""PAPER-ONLY Solana memecoin desk. No wallets, no keys, no signing, no real money.
Read-only public APIs: DexScreener, GeckoTerminal, RugCheck. Strict thresholds copied from @x_insider4 (experiment).
Two paper desks share one scout pass per cycle: STRICT (the post's rules) and LOOSE (looser rules, own $500 bank).
A third paper desk, PUMP (pump.py: pump.fun bonding-curve momentum, own $500 bank), runs in a parallel thread each cycle."""
import json, os, sys, time, threading, datetime as dt
import requests
import pump

HERE = os.path.dirname(os.path.abspath(__file__))
DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
RC = "https://api.rugcheck.xyz/v1/tokens/{}/report"
CACHE = os.path.join(HERE, "scout_cache.json")   # runner-local (gitignored): RugCheck cache + per-desk seen times
START_BANK = 500.0
HAIRCUT = 0.02              # slippage+fee per side (both desks)
DEADLINE = 45               # seconds per cycle (both desks) so a 1-minute loop keeps its cadence
MAX_CANDIDATES = 200        # due mints per cycle (pair data is batched 30/call, cheap)
RC_BUDGET = 40              # max fresh RugCheck calls per cycle (limit is ~15 per short window)
RC_TTL = 30 * 60            # reuse a RugCheck report for 30 min
RC_NEG_TTL = 5 * 60         # don't retry a failed RugCheck fetch for 5 min
REVISIT = 15 * 60           # a rejected coin is re-scored after 15 min (its volume/holders change)
SEARCH_MAX_AGE = 3 * 86400  # search results: pairs < 3 days old
EXCLUDE = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # SOL, USDC, USDT
S = requests.Session(); S.headers["User-Agent"] = "paper-desk/1.1"

# ---------------- DESK PROFILES (all tunables live here) ----------------
PROFILES = {
    "strict": dict(label="STRICT", state="state.json", log="log.jsonl",
                   mint_revoked=True, freeze_revoked=False, lp_min=95, top10_max=20, vol_h1_min=50_000, liq_min=None,
                   bad_dev="any",            # rugged OR danger flag mentioning dev/insider/rug/... kills
                   missing_ok=(),            # every missing field is a fail
                   analyst_floor=0.60, buy_p=0.70, organic_p=0.65, exit_risk_max=3,
                   size_pct=0.08, max_open=None, stop=-0.06, trail_arm=0.12, trail_giveback=0.10,
                   time_stop_h=None, time_stop_min_ret=None),
    "loose": dict(label="LOOSE", state="state_loose.json", log="log_loose.jsonl",
                  mint_revoked=True, freeze_revoked=True, lp_min=80, top10_max=35, vol_h1_min=10_000, liq_min=10_000,
                  bad_dev="rugged",          # only RugCheck's 'rugged' verdict kills; danger flags are logged, not fatal
                  missing_ok=("lp_locked", "top10_pct", "bad_dev"),   # non-critical: missing = neutral
                  analyst_floor=0.45, buy_p=0.55, organic_p=0.50, exit_risk_max=4,
                  size_pct=0.05, max_open=6, stop=-0.08, trail_arm=0.10, trail_giveback=0.10,
                  time_stop_h=6, time_stop_min_ret=0.03),
}
P = PROFILES["strict"]   # legacy default (manual account / scoreboard)
STATE = os.path.join(HERE, P["state"]); LOG = os.path.join(HERE, P["log"])
NET = dict(calls=0, http429=0, errors=0, by={})

def now(): return time.time()
def iso(t=None): return dt.datetime.fromtimestamp(t or now()).isoformat(timespec="seconds")
def spath(p): return os.path.join(HERE, p["state"])
def lpath(p): return os.path.join(HERE, p["log"])

def load(p=None):
    p = p or P
    if os.path.exists(spath(p)):
        st = json.load(open(spath(p))); st.pop("seen", None); return st   # 'seen' now lives in the runner cache
    return {"bank_start": START_BANK, "cash": START_BANK, "positions": {}, "watch": {},
            "traded": [], "equity": [], "cycles": 0, "started": iso()}
def save(st, p=None):
    f = spath(p or P); tmp = f + ".tmp"; json.dump(st, open(tmp, "w"), indent=1); os.replace(tmp, f)
def log(kind, p=None, **kw):
    with open(lpath(p or P), "a") as f: f.write(json.dumps({"ts": iso(), "t": now(), "kind": kind, **kw}) + "\n")
def load_cache():
    try: c = json.load(open(CACHE))
    except Exception: c = {}
    c.setdefault("rc", {}); c.setdefault("seen", {})
    for k in PROFILES: c["seen"].setdefault(k, {})
    cut = now() - 3 * 86400
    for k in c["seen"]: c["seen"][k] = {a: v for a, v in c["seen"][k].items() if v[0] > cut}
    c["rc"] = {a: v for a, v in c["rc"].items() if v["t"] > now() - RC_TTL}
    return c
def save_cache(c):
    tmp = CACHE + ".tmp"; json.dump(c, open(tmp, "w")); os.replace(tmp, CACHE)

def get(url, timeout=12, headers=None, tag=None):
    tag = tag or url.split("/")[2]
    NET["calls"] += 1; b = NET["by"].setdefault(tag, [0, 0]); b[0] += 1
    try:
        r = S.get(url, timeout=timeout, headers=headers or {})
        if r.status_code == 429: NET["http429"] += 1; b[1] += 1; return None
        return r.json() if r.status_code == 200 else None
    except Exception: NET["errors"] += 1; return None

# ---------------- SEAT 1: SCOUT (shared by both desks) ----------------
def scout():
    """Return {mint: [source tags]} of Solana candidates from free no-key sources, deduped by mint."""
    found = {}
    def add(a, src):
        if a and a not in EXCLUDE: found.setdefault(a, []).append(src)
    for ep, tag in (("/token-profiles/latest/v1", "ds_profiles"), ("/token-boosts/latest/v1", "ds_boosts"),
                    ("/token-boosts/top/v1", "ds_boosts_top"), ("/community-takeovers/latest/v1", "ds_cto"),
                    ("/ads/latest/v1", "ds_ads")):
        for x in get(DS + ep, tag="dexscreener") or []:
            if isinstance(x, dict) and x.get("chainId") == "solana": add(x.get("tokenAddress"), tag)
    for q in ("pumpswap", "raydium", "pump", "meteora", "bonk", "moonshot", "launchlab", "solana meme"):
        for p in (get(f"{DS}/latest/dex/search?q={q}", tag="dexscreener") or {}).get("pairs") or []:
            if p.get("chainId") == "solana" and (p.get("pairCreatedAt") or 0) > (now() - SEARCH_MAX_AGE) * 1000:
                add(p["baseToken"]["address"], "ds_search")
    H = {"Accept": "application/json;version=20230302"}
    for ep, tag in (("/networks/solana/new_pools?page=1", "gt_new"), ("/networks/solana/new_pools?page=2", "gt_new"),
                    ("/networks/solana/trending_pools?duration=1h", "gt_trending")):   # 3 calls/min (limit ~30/min)
        for pool in (get(GT + ep, headers=H, tag="geckoterminal") or {}).get("data") or []:
            rel = pool.get("relationships") or {}
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "").split("_", 1)[-1]
            quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "").split("_", 1)[-1]
            add(quote if base in EXCLUDE else base, tag)
    return found

def best_pairs(addrs):
    """Map token -> its most liquid Solana pair (DexScreener /tokens/v1, 30 per call)."""
    out = {}; addrs = list(dict.fromkeys(addrs)); want = set(addrs)
    for i in range(0, len(addrs), 30):
        for p in get(f"{DS}/tokens/v1/solana/{','.join(addrs[i:i+30])}", tag="dexscreener") or []:
            a = (p.get("baseToken") or {}).get("address")
            if a in want and (a not in out or ((p.get("liquidity") or {}).get("usd") or 0) > ((out[a].get("liquidity") or {}).get("usd") or 0)):
                out[a] = p
    return out

def rugcheck(addr, cache, budget):
    """Cached RugCheck report. budget = dict(left=int, hit=0, fresh=0, stop=False)."""
    c = cache["rc"].get(addr)
    if c and now() - c["t"] < (RC_TTL if c["r"] is not None else RC_NEG_TTL):
        budget["hit"] += 1; return c["r"], True
    if budget["stop"] or budget["left"] <= 0: return None, False          # deferred, not evaluated
    budget["left"] -= 1; budget["fresh"] += 1
    try:
        r = S.get(RC.format(addr), timeout=15); NET["calls"] += 1; b = NET["by"].setdefault("rugcheck", [0, 0]); b[0] += 1
        if r.status_code == 429:
            NET["http429"] += 1; b[1] += 1; budget["stop"] = True; budget["left"] += 1; budget["fresh"] -= 1
            return None, False
        rc = r.json() if r.status_code == 200 else None
        rem = r.headers.get("X-Rate-Limit-Remaining")
        time.sleep(1.2 if rem is not None and rem.isdigit() and int(rem) <= 3 else 0.25)
    except Exception: NET["errors"] += 1; rc = None
    slim = None
    if rc is not None:   # keep only what RISK needs, so the cache stays small
        slim = {k: rc.get(k) for k in ("mintAuthority", "freezeAuthority", "rugged", "graphInsidersDetected", "totalHolders", "score_normalised")}
        slim["markets"] = [{"pubkey": m.get("pubkey"), "lp": {"lpLockedPct": (m.get("lp") or {}).get("lpLockedPct")} if m.get("lp") else None}
                           for m in rc.get("markets") or []]
        skip = {k for k, v in (rc.get("knownAccounts") or {}).items() if (v or {}).get("type") in ("AMM", "LOCKER")}
        skip |= {m.get("pubkey") for m in rc.get("markets") or []}
        th = rc.get("topHolders")
        slim["top10_pct"] = None if th is None else round(sum(h.get("pct", 0) for h in
                              [h for h in th if h.get("owner") not in skip and h.get("address") not in skip][:10]), 2)
        slim["danger"] = [x.get("name", "") for x in rc.get("risks") or [] if x.get("level") == "danger"]
    cache["rc"][addr] = {"t": now(), "r": slim}
    return slim, True

# ---------------- SEAT 2: FILTER / RISK ----------------
BAD_RISK_WORDS = ("rug", "creator history", "dev", "insider", "honeypot", "freeze", "copycat", "scam")
def facts(pair, rc):
    f = {}
    if rc is None:
        f.update(mint_revoked="missing", freeze_revoked="missing", lp_locked_pct="missing", top10_pct="missing", bad_dev="missing")
    else:
        f["mint_revoked"] = rc.get("mintAuthority") is None
        f["freeze_revoked"] = rc.get("freezeAuthority") is None
        mk = [m for m in rc.get("markets") or [] if m.get("pubkey") == pair.get("pairAddress")] or (rc.get("markets") or [])
        lp = (mk[0].get("lp") or {}) if mk else {}
        v = lp.get("lpLockedPct") if mk else None
        f["lp_locked_pct"] = "missing" if v is None else v
        f["top10_pct"] = "missing" if rc.get("top10_pct") is None else rc["top10_pct"]
        f["danger_flags"] = rc.get("danger") or []
        f["rugged"] = bool(rc.get("rugged"))
        f["bad_dev_any"] = f["rugged"] or any(w in n.lower() for n in f["danger_flags"] for w in BAD_RISK_WORDS)
        f["insiders_detected"] = rc.get("graphInsidersDetected", "missing")
        f["holders"] = rc.get("totalHolders", "missing")
        f["rc_score"] = rc.get("score_normalised", "missing")
    v = (pair.get("volume") or {}).get("h1"); f["vol_h1"] = "missing" if v is None else v
    v = (pair.get("liquidity") or {}).get("usd"); f["liq_usd"] = "missing" if v is None else v
    return f

def pair_fails(p, f):
    """Checks that need only DexScreener data (run before spending a RugCheck call)."""
    fails = []
    if f["vol_h1"] == "missing": fails.append("vol_h1=missing")
    elif f["vol_h1"] < p["vol_h1_min"]: fails.append(f"vol_h1={f['vol_h1']}")
    if p["liq_min"] is not None:
        if f["liq_usd"] == "missing": fails.append("liq_usd=missing")
        elif f["liq_usd"] < p["liq_min"]: fails.append(f"liq_usd={f['liq_usd']}")
    return fails

def risk_fails(p, f):
    fails = []
    def chk(name, val, ok):
        if val == "missing":
            if name not in p["missing_ok"]: fails.append(f"{name}=missing")
        elif not ok(val): fails.append(f"{name}={val}")
    chk("mint_revoked", f["mint_revoked"], lambda v: v is True)
    if p["freeze_revoked"]: chk("freeze_revoked", f["freeze_revoked"], lambda v: v is True)
    chk("lp_locked", f["lp_locked_pct"], lambda v: v >= p["lp_min"])
    chk("top10_pct", f["top10_pct"], lambda v: v <= p["top10_max"])
    bd = f.get("bad_dev_any" if p["bad_dev"] == "any" else "rugged", "missing") if f["mint_revoked"] != "missing" else "missing"
    chk("bad_dev", bd, lambda v: v is False)
    return pair_fails(p, f) + fails   # one hard fail = dead

def risk(addr, pair):
    """Strict RISK verdict for one mint (used by the manual account; not cached)."""
    c = {"rc": {}}; rc, _ = rugcheck(addr, c, dict(left=1, hit=0, fresh=0, stop=False))
    f = facts(pair, rc); return f, risk_fails(PROFILES["strict"], f)

# ---------------- SEAT 3: ANALYST ----------------
def clamp(x, a=0.0, b=1.0): return max(a, min(b, x))
def analyst(pair, f):
    tx = (pair.get("txns") or {}).get("h1") or {}
    b, s = tx.get("buys", 0), tx.get("sells", 0)
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    vol = f["vol_h1"] if isinstance(f["vol_h1"], (int, float)) else 0
    pc = pair.get("priceChange") or {}
    parts = {
        "buy_pressure": clamp((b / max(b + s, 1) - 0.4) / 0.3),            # 40%->0, 70%->1
        "participation": clamp((b + s) / 1500),                              # txn count
        "liquidity": clamp(liq / 150_000),
        "turnover_sane": 1.0 if 0.3 <= vol / max(liq, 1) <= 5 else 0.3,      # wash-ish if extreme
        "not_parabolic": clamp(1 - max(pc.get("h1", 0) or 0, 0) / 300),
        "holder_spread": clamp(1 - (f["top10_pct"] if isinstance(f["top10_pct"], (int, float)) else 20) / 20),
        "socials": 1.0 if (pair.get("info") or {}).get("socials") else 0.0,
    }
    w = {"buy_pressure": .2, "participation": .15, "liquidity": .2, "turnover_sane": .15,
         "not_parabolic": .1, "holder_spread": .1, "socials": .1}
    return round(sum(parts[k] * w[k] for k in w), 3), parts

# ---------------- SEAT 4: JUDGE (deterministic, closed answers only) ----------------
def dossier(addr, pair, f):
    tx = (pair.get("txns") or {}).get("h1") or {}
    pc = pair.get("priceChange") or {}
    def m(v): return "missing" if v is None else v
    return {"token": addr, "symbol": m(pair.get("baseToken", {}).get("symbol")), "dex": m(pair.get("dexId")),
            "price_usd": m(pair.get("priceUsd")), "liq_usd": m((pair.get("liquidity") or {}).get("usd")),
            "mcap": m(pair.get("marketCap")), "vol_h1": f["vol_h1"], "buys_h1": m(tx.get("buys")), "sells_h1": m(tx.get("sells")),
            "chg_h1": m(pc.get("h1")), "chg_h6": m(pc.get("h6")), "chg_h24": m(pc.get("h24")),
            "age_h": round((now() * 1000 - pair["pairCreatedAt"]) / 3.6e6, 1) if pair.get("pairCreatedAt") else "missing",
            "top10_pct": f["top10_pct"], "holders": f.get("holders", "missing"),
            "insiders_detected": f.get("insiders_detected", "missing"), "lp_locked_pct": f.get("lp_locked_pct", "missing")}
def num(v, d): return v if isinstance(v, (int, float)) else d
def judge(d, p=None):
    p = p or P
    b, s = num(d["buys_h1"], 0), num(d["sells_h1"], 0)
    ratio = b / max(b + s, 1); liq = num(d["liq_usd"], 0); vol = num(d["vol_h1"], 0)
    holders = num(d["holders"], 0); turn = vol / max(liq, 1)
    p_org = 0.5 + 0.25 * clamp((ratio - 0.5) / 0.2) + 0.15 * clamp(holders / 2000) \
            - (0.25 if d["insiders_detected"] not in (False, 0, "missing") else 0) \
            - (0.2 if turn > 5 else 0) - (0.1 if "missing" in (d["holders"], d["insiders_detected"]) else 0)
    p_org = round(clamp(p_org, 0.02, 0.98), 2)
    run = max(num(d["chg_h6"], 0), num(d["chg_h24"], 0))
    p_late = round(clamp(0.15 + run / 1000 + (0.2 if num(d["chg_h1"], 0) > 100 else 0) + (0.15 if num(d["age_h"], 0) > 24 else 0), 0.02, 0.98), 2)
    late = p_late >= 0.5
    exit_risk = 1 + (liq < 50_000) + (liq < 20_000) + (num(d["top10_pct"], 20) > 15) + (num(d["mcap"], 0) / max(liq, 1) > 15)
    exit_risk = int(min(exit_risk, 5))
    p_buy = round(clamp(0.35 + 0.4 * (p_org - 0.5) * 2 + 0.25 * (1 - p_late) - 0.08 * (exit_risk - 1), 0.02, 0.98), 2)
    action = "buy" if p_buy >= 0.5 else ("watch" if p_buy >= 0.35 else "pass")
    a = {"organic": "yes" if p_org >= 0.5 else "no", "p_organic": p_org, "late": "yes" if late else "no",
         "p_late": p_late, "exit_risk": exit_risk, "action": action, "p_action": p_buy if action == "buy" else round(1 - p_buy, 2), "p_buy": p_buy}
    a["gate"] = (action == "buy" and p_buy >= p["buy_p"] and a["organic"] == "yes" and p_org >= p["organic_p"]
                 and not late and exit_risk <= p["exit_risk_max"])
    return a

# ---------------- SEAT 5: EXECUTOR (paper) ----------------
def equity(st, prices):
    return st["cash"] + sum(p["qty"] * prices.get(k, p["last"]) * (1 - HAIRCUT) for k, p in st["positions"].items())
def paper_buy(st, addr, d, j, p):
    price = float(d["price_usd"]); bal = equity(st, {})
    size = round(min(p["size_pct"] * bal, st["cash"]), 2)       # confidence never changes size
    if size < 1: return log("reject", p, token=addr, seat="EXECUTOR", reason="no cash")
    fill = price * (1 + HAIRCUT); qty = size / fill
    st["cash"] -= size; st["traded"].append(addr)
    st["positions"][addr] = {"sym": d["symbol"], "qty": qty, "entry": fill, "mark_entry": price, "cost": size,
                             "peak": price, "last": price, "opened": now(), "p_buy": j["p_buy"]}
    log("entry", p, desk=p["label"], token=addr, sym=d["symbol"], price=price, fill=fill, size=size, p_buy=j["p_buy"], judge=j)
    return True

# ---------------- SEAT 6: RISK / EXIT ----------------
def manage(st, stats, p, pairs):
    prices = {}
    for a, pos in list(st["positions"].items()):
        pr = pairs.get(a, {}).get("priceUsd")
        if pr is None: stats["stale"] += 1; continue
        pr = float(pr); prices[a] = pr; pos["last"] = pr; pos["peak"] = max(pos["peak"], pr)
        exit_px = pr * (1 - HAIRCUT); ret = exit_px / pos["entry"] - 1
        peak_ret = pos["peak"] / pos["mark_entry"] - 1
        reason = None
        if ret <= p["stop"]: reason = f"hard stop {ret:+.1%}"
        elif peak_ret >= p["trail_arm"] and pr <= pos["peak"] * (1 - p["trail_giveback"]): reason = f"trail hit (peak {peak_ret:+.1%})"
        elif p["time_stop_h"] and now() - pos["opened"] >= p["time_stop_h"] * 3600 and ret < p["time_stop_min_ret"]:
            reason = f"time stop {p['time_stop_h']}h ({ret:+.1%})"
        if reason:
            proceeds = pos["qty"] * exit_px; pnl = proceeds - pos["cost"]
            st["cash"] += proceeds; del st["positions"][a]
            stats["exits"] += 1; stats["win" if pnl > 0 else "loss"] += 1
            log("exit", p, desk=p["label"], token=a, sym=pos["sym"], price=pr, fill=exit_px, pnl=round(pnl, 2), ret=round(ret, 4),
                reason=reason, p_buy=pos["p_buy"], held_min=round((now() - pos["opened"]) / 60))
    return prices

# ---------------- CYCLE ----------------
def due_list(name, st, found, cache):
    seen = cache["seen"][name]
    w = [a for a in st["watch"]]
    new = [a for a in found if a not in seen and a not in st["positions"] and a not in w]
    old = sorted((a for a in found if a in seen and a not in st["positions"] and a not in w and now() - seen[a][0] >= REVISIT),
                 key=lambda a: seen[a][0])
    return list(dict.fromkeys(w + new + old))

def evaluate(name, p, st, a, pair, rc, stats, cache, lost, rc_checked=True):
    seen = cache["seen"][name]; prev = seen.get(a, [0, None])[1]
    f = facts(pair, rc); sym = pair["baseToken"].get("symbol"); price = pair.get("priceUsd")
    def rej(seat, reason, **kw):
        seen[a] = [now(), reason[:160]]
        if reason[:160] != prev: log("reject", p, token=a, sym=sym, seat=seat, reason=reason, price=price, **kw)   # unchanged reasons aren't re-logged
    stats["leads"] += 1
    fails = risk_fails(p, f) if rc_checked else pair_fails(p, f)   # skipped RugCheck only when pair data already fails
    if fails:
        stats["risk_killed"] += 1; st["watch"].pop(a, None)
        return rej("RISK", "; ".join(fails), facts={k: f[k] for k in ("mint_revoked", "freeze_revoked", "lp_locked_pct", "top10_pct", "vol_h1", "liq_usd") if k in f})
    score, parts = analyst(pair, f)
    if score < p["analyst_floor"]:
        stats["analyst_killed"] += 1; st["watch"].pop(a, None)
        return rej("ANALYST", f"score {score} < {p['analyst_floor']}")
    d = dossier(a, pair, f); j = judge(d, p)
    room = p["max_open"] is None or len(st["positions"]) < p["max_open"]
    if j["gate"] and a not in st["traded"] and not lost and a not in st["positions"] and room:
        stats["buy"] += 1; st["watch"].pop(a, None); seen[a] = [now(), "bought"]
        if paper_buy(st, a, d, j, p): stats["entries"] += 1
        return
    if j["action"] == "watch" and a not in st["watch"]:
        stats["watch"] += 1; st["watch"][a] = now(); seen[a] = [now(), "watch"]
        return log("watch", p, token=a, sym=sym, judge=j, dossier=d, price=price)
    why = ("second watch -> pass" if a in st["watch"] else "gate not met" if not j["gate"]
           else "max open positions" if not room else "no re-entry / revenge guard")
    st["watch"].pop(a, None); stats["pass_"] += 1; stats["clean_pass"] += 1
    rej("JUDGE", why, judge=j, risk_clean=True)

def _pump_cycle(out):
    try: out.append(pump.cycle())
    except Exception as ex: out.append(f"CYCLE PUMP ERROR {iso()} | {type(ex).__name__}: {ex}")

def cycle(only=None):
    t0 = now(); NET.update(calls=0, http429=0, errors=0, by={})
    if only == "pump":                       # PUMP desk alone (own data path, own bank)
        out = []; _pump_cycle(out); print(out[0]); terminal(); print(f"CYCLE_SECS {round(now() - t0, 1)}"); return
    pump_out = []; pump_thread = None
    if only is None:                         # PUMP runs alongside STRICT/LOOSE (separate session, files and bank)
        pump_thread = threading.Thread(target=_pump_cycle, args=(pump_out,)); pump_thread.start()
    names = [only] if only else list(PROFILES)
    cache = load_cache(); desks = {}
    for n in names:
        st = load(PROFILES[n]); st["cycles"] += 1
        desks[n] = dict(st=st, stats=dict(leads=0, risk_killed=0, analyst_killed=0, buy=0, watch=0, pass_=0, clean_pass=0,
                                          entries=0, exits=0, win=0, loss=0, stale=0, deferred=0))
    # 1) exits first: re-price every open position every cycle
    held = [a for n in names for a in desks[n]["st"]["positions"]]
    pos_pairs = best_pairs(held) if held else {}
    for n in names:
        D = desks[n]; D["prices"] = manage(D["st"], D["stats"], PROFILES[n], pos_pairs); D["lost"] = D["stats"]["loss"] > 0  # no revenge trades
    # 2) shared scout pass
    found = scout(); t_scout = now() - t0
    due = {n: due_list(n, desks[n]["st"], found, cache) for n in names}
    union = list(dict.fromkeys(a for n in names for a in due[n]))[:MAX_CANDIDATES]
    pairs = best_pairs(union)
    budget = dict(left=RC_BUDGET, hit=0, fresh=0, stop=False)
    for a in union:
        if now() - t0 > DEADLINE: break
        pair = pairs.get(a)
        if not pair or pair.get("chainId") != "solana":
            for n in names:
                if a in due[n]: cache["seen"][n][a] = [now(), "no pair"]
            continue
        f0 = facts(pair, None)
        want = [n for n in names if a in due[n]]
        need_rc = [n for n in want if not pair_fails(PROFILES[n], f0)]
        rc, ok = (rugcheck(a, cache, budget) if need_rc else (None, True))
        for n in want:
            if n in need_rc and not ok: desks[n]["stats"]["deferred"] += 1; continue   # RugCheck budget spent: retry next cycle
            D = desks[n]; evaluate(n, PROFILES[n], D["st"], a, pair, rc if n in need_rc else None, D["stats"], cache, D["lost"], n in need_rc)
    save_cache(cache)
    secs = round(now() - t0, 1)
    src = {}
    for a, tags in found.items():
        for t in set(tags): src[t] = src.get(t, 0) + 1
    net = dict(calls=NET["calls"], http429=NET["http429"], errors=NET["errors"], by=NET["by"], rc_fresh=budget["fresh"], rc_cache_hits=budget["hit"])
    print(f"SCOUT {iso()} | pool {len(found)} unique mints {src} | due {len(union)} | RugCheck fresh {budget['fresh']} cached {budget['hit']} "
          f"| http {NET['calls']} calls, 429s {NET['http429']} {({k: v[1] for k, v in NET['by'].items() if v[1]})} | scout {t_scout:.1f}s")
    for n in names:
        p = PROFILES[n]; D = desks[n]; st, stats = D["st"], D["stats"]
        eq = round(equity(st, D["prices"]), 2)
        E = st["equity"]
        if E and E[-1][1] == eq and len(E) > 1 and E[-2][1] == eq and now() - dt.datetime.fromisoformat(E[-2][0]).timestamp() < 600:
            E[-1] = [iso(), eq]          # flat equity: keep one point per 10 min so state stays small
        else: E.append([iso(), eq])
        st["equity"] = E[-3000:]; save(st, p)
        log("cycle", p, desk=p["label"], stats=stats, pool=len(found), sources=src, net=net, equity=eq, cash=round(st["cash"], 2),
            open=len(st["positions"]), secs=secs)
        print(f"CYCLE {p['label']} {st['cycles']} {iso()} | leads {stats['leads']} (deferred {stats['deferred']}) | RISK killed {stats['risk_killed']} "
              f"| ANALYST killed {stats['analyst_killed']} | JUDGE buy/watch/pass {stats['buy']}/{stats['watch']}/{stats['pass_']} "
              f"| exits {stats['exits']} (W{stats['win']}/L{stats['loss']}) | open {len(st['positions'])} | equity ${eq:.2f} | {secs:.0f}s")
    if pump_thread:
        pump_thread.join(); print(pump_out[0] if pump_out else f"CYCLE PUMP ERROR {iso()} | no result"); secs = round(now() - t0, 1)
    terminal()
    print(f"CYCLE_SECS {secs}")

# ---------------- CAELAN'S MANUAL PAPER ACCOUNT (separate state; paper only) ----------------
MINE_STATE = os.path.join(HERE, "mine_state.json")
MINE_LOG = os.path.join(HERE, "mine_log.jsonl")
def mine_load():
    if os.path.exists(MINE_STATE): return json.load(open(MINE_STATE))
    return {"owner": "caelan", "bank_start": START_BANK, "cash": START_BANK, "positions": {}, "realized": 0.0, "trades": 0, "wins": 0, "losses": 0}
def mine_save(m):
    tmp = MINE_STATE + ".tmp"; json.dump(m, open(tmp, "w"), indent=1); os.replace(tmp, MINE_STATE)
def mine_log(kind, **kw):
    with open(MINE_LOG, "a") as f: f.write(json.dumps({"ts": iso(), "t": now(), "owner": "caelan", "kind": kind, **kw}) + "\n")
def resolve(q):
    """Symbol or mint -> (mint, best Solana pair). Highest-liquidity pair wins."""
    if len(q) >= 32:
        pr = best_pairs([q]).get(q)
        return (q, pr) if pr else (None, None)
    pairs = [p for p in (get(f"{DS}/latest/dex/search?q={q}") or {}).get("pairs") or []
             if p.get("chainId") == "solana" and (p.get("baseToken", {}).get("symbol") or "").upper() == q.upper().lstrip("$")]
    if not pairs: return None, None
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    mints = {p["baseToken"]["address"] for p in pairs}
    if len(mints) > 1: print(f"'{q}' matched {len(mints)} Solana mints; chose highest liquidity.")
    return best["baseToken"]["address"], best
def mine_prices(m):
    pairs = best_pairs(list(m["positions"])) if m["positions"] else {}
    return {a: float(pairs[a]["priceUsd"]) for a in m["positions"] if pairs.get(a, {}).get("priceUsd")}
def mine_equity(m, prices):
    return m["cash"] + sum(p["qty"] * prices.get(a, p["last"]) * (1 - HAIRCUT) for a, p in m["positions"].items())
def mine(args):
    if not args: return print("usage: desk.py mine buy <sym|mint> <usd> | sell <sym|mint> [pct] | status")
    m = mine_load(); cmd = args[0]
    if cmd == "buy":
        if len(args) < 3: return print("usage: desk.py mine buy <sym|mint> <usd>")
        usd = float(args[2])
        if usd <= 0: return print("REJECTED: amount must be > 0")
        if usd > m["cash"] + 1e-9: return print(f"REJECTED: ${usd:.2f} > paper cash ${m['cash']:.2f}")
        mint, pair = resolve(args[1])
        if not pair or not pair.get("priceUsd"): return print(f"REJECTED: no live Solana price for '{args[1]}'")
        price = float(pair["priceUsd"]); fill = price * (1 + HAIRCUT); qty = usd / fill; sym = pair["baseToken"]["symbol"]
        print(f"Resolved {args[1]} -> {sym} mint {mint} ({pair.get('dexId')}, liq ${((pair.get('liquidity') or {}).get('usd') or 0):,.0f})")
        f, fails = risk(mint, pair)
        print("Desk RISK verdict (info only, not blocking): " + ("PASS" if not fails else "WOULD KILL -> " + "; ".join(fails)))
        p = m["positions"].get(mint)
        if p:
            p["qty"] += qty; p["cost"] += usd; p["entry"] = p["cost"] / p["qty"]; p["last"] = price
        else:
            m["positions"][mint] = {"sym": sym, "qty": qty, "cost": usd, "entry": fill, "last": price, "opened": now()}
        m["cash"] -= usd; mine_save(m)
        mine_log("entry", token=mint, sym=sym, price=price, fill=fill, size=usd, qty=qty, risk_fails=fails)
        print(f"PAPER BUY {sym}: ${usd:.2f} @ {price:.8g} (fill {fill:.8g} w/ 2% haircut), qty {qty:,.4f}. Cash ${m['cash']:.2f}")
    elif cmd == "sell":
        if len(args) < 2: return print("usage: desk.py mine sell <sym|mint> [pct]")
        q = args[1]; pct = float(args[2]) if len(args) > 2 else 100.0
        if not 0 < pct <= 100: return print("REJECTED: pct must be 1-100")
        key = q if q in m["positions"] else next((a for a, p in m["positions"].items() if p["sym"].upper() == q.upper().lstrip("$")), None)
        if not key: return print(f"REJECTED: no open paper position in '{q}'")
        pr = mine_prices({"positions": {key: m["positions"][key]}}).get(key)
        if pr is None: return print("REJECTED: no live price right now; try again")
        p = m["positions"][key]; frac = pct / 100; qty = p["qty"] * frac; cost = p["cost"] * frac
        fill = pr * (1 - HAIRCUT); proceeds = qty * fill; pnl = proceeds - cost
        m["cash"] += proceeds; m["realized"] += pnl; m["trades"] += 1; m["wins" if pnl > 0 else "losses"] += 1
        if pct >= 100: del m["positions"][key]
        else: p["qty"] -= qty; p["cost"] -= cost; p["last"] = pr
        mine_save(m); mine_log("exit", token=key, sym=p["sym"], price=pr, fill=fill, pct=pct, proceeds=proceeds, pnl=pnl)
        print(f"PAPER SELL {pct:g}% {p['sym']} @ {pr:.8g} (fill {fill:.8g}): proceeds ${proceeds:.2f}, P&L ${pnl:+.2f}. Cash ${m['cash']:.2f}")
    elif cmd == "status":
        prices = mine_prices(m)
        for a, p in m["positions"].items():
            pr = prices.get(a, p["last"]); val = p["qty"] * pr * (1 - HAIRCUT)
            print(f"  {p['sym']:<10} cost ${p['cost']:.2f}  value ${val:.2f}  P&L ${val - p['cost']:+.2f} ({val / p['cost'] - 1:+.1%})  {a}")
        if not m["positions"]: print("  (no open positions)")
        eq = mine_equity(m, prices)
        print(f"CAELAN PAPER: cash ${m['cash']:.2f} | equity ${eq:.2f} (start ${m['bank_start']:.2f}, {eq / m['bank_start'] - 1:+.2%}) | realized ${m['realized']:+.2f} | closed trades {m['trades']} W/L {m['wins']}/{m['losses']}")
    else: print("usage: desk.py mine buy|sell|status")

def score_rows(live=True):
    m = mine_load(); st = load(); L = read_log()
    ex = [e for e in L if e["kind"] == "exit"]
    b_eq = st["equity"][-1][1] if st["equity"] else st["bank_start"]
    c_eq = mine_equity(m, mine_prices(m) if live else {})
    return [("Caelan", m["bank_start"], c_eq, m["trades"], m["wins"], m["losses"], len(m["positions"])),
            ("Bot", st["bank_start"], b_eq, sum(e["kind"] == "entry" for e in L), sum(e["pnl"] > 0 for e in ex), sum(e["pnl"] <= 0 for e in ex), len(st["positions"]))] \
           + [bot_row("Loose", PROFILES["loose"]), pump.row()]
def bot_row(name, p):
    st = load(p); L = read_log(p); ex = [e for e in L if e["kind"] == "exit"]
    eq = st["equity"][-1][1] if st["equity"] else st["bank_start"]
    return (name, st["bank_start"], eq, sum(e["kind"] == "entry" for e in L), sum(e["pnl"] > 0 for e in ex), sum(e["pnl"] <= 0 for e in ex), len(st["positions"]))
def scoreboard(live=True):
    print("CAELAN vs BOT (paper)\nwho      start     equity    change   trades  W/L   open")
    for n, s0, e, t, w, l, o in score_rows(live):
        print(f"{n:<8} ${s0:<8.2f} ${e:<8.2f} {e / s0 - 1:+7.2%}  {t:>5}   {w}/{l}   {o}")
    print("(Bot = STRICT desk, Loose = LOOSE desk, Pump = PUMP desk. Caelan trades = closed sells; Bot trades = entries. Equity marked at live price minus 2% exit haircut; "
          "Pump marked at its curve sell quote after fees+slippage; Pump W/L counts fully closed trades.)")

# ---------------- SEAT 7: LOGGER readers ----------------
def read_log(p=None):
    f = lpath(p or P)
    return [json.loads(l) for l in open(f)] if os.path.exists(f) else []

def recap_desk(p):
    st = load(p); L = read_log(p); since = now() - 86400
    R = [e for e in L if e["t"] >= since]
    rej = [e for e in R if e["kind"] == "reject"]
    rk = [e for e in rej if e.get("seat") == "RISK"]
    jp = [e for e in rej if e.get("seat") == "JUDGE"]
    ent = [e for e in R if e["kind"] == "entry"]; ex = [e for e in R if e["kind"] == "exit"]
    w = sum(e["pnl"] > 0 for e in ex); l = len(ex) - w
    cyc = [e for e in R if e["kind"] == "cycle"]
    leads = sum(e["stats"]["leads"] for e in cyc)
    watch = sum(e["kind"] == "watch" for e in R)
    eq = st["equity"][-1][1] if st["equity"] else st["bank_start"]
    from collections import Counter
    c = Counter(r.split("=")[0] for e in rk for r in e["reason"].split("; "))
    worked = (f"RISK gate held: {len(rk)} distinct kills before any money moved (top reason: {c.most_common(1)[0][0]})" if c
              else "No trades forced; desk stayed in cash")
    if ex: worked = f"{w}/{len(ex)} closed trades green; stops kept losers small" if w else worked
    missing = sum(any(r.endswith("=missing") and not r.startswith(("vol_h1", "liq_usd")) for r in e["reason"].split("; ")) for e in rk)
    change = (f"{missing} kills were on 'missing' data - consider a 2nd data source" if rk and missing > len(rk) / 3
              else f"Most kills are '{c.most_common(1)[0][0]}' - scout is surfacing mostly un-tradeable pairs" if c
              else "Need more cycles before changing anything")
    pnl = sum(e["pnl"] for e in ex)
    print(f"== {p['label']} DESK ({p['state']}) ==")
    print(f"DESK {p['label']} {dt.date.today()} / cycles {len(cyc)} / lead checks {leads} / killed by RISK {len(rk)} / JUDGE buy/watch/pass {len(ent)}/{watch}/{len(jp)} "
          f"/ trades executed {len(ent)} W/L {w}/{l} (realized ${pnl:+.2f}) / bank ${st['bank_start']:.2f}->${eq:.2f} ({eq / st['bank_start'] - 1:+.2%}) "
          f"/ worked: {worked} / change: {change}")
    for e in ent: print(f"  BUY  {e['sym']} ${e['size']:.2f} @ {e['price']} ({e['ts']})")
    for e in ex: print(f"  SELL {e['sym']} P&L ${e['pnl']:+.2f} ({e['ret']*100:+.1f}%) {e['reason']} ({e['ts']})")
    for a, pos in st["positions"].items(): print(f"  OPEN {pos['sym']} cost ${pos['cost']:.2f} last {pos['last']} ({pos['last']*(1-HAIRCUT)/pos['entry']-1:+.1%})")
    print(f"  (open positions: {len(st['positions'])}, cash ${st['cash']:.2f}; RISK kill reasons: {dict(c)})")

def recap():
    for n in PROFILES: recap_desk(PROFILES[n]); print()
    pump.recap(); print()
    scoreboard()
def calibrate(p=None):
    p = p or P; print(f"[{p['label']} desk]")
    L = read_log(p); since = now() - 7 * 86400
    ent = {e["token"]: e for e in L if e["kind"] == "entry" and e["t"] >= since}
    ex = {e["token"]: e for e in L if e["kind"] == "exit"}
    st = load(p)
    rows = []
    for lo, hi, name in ((0.70, 0.80, "0.70-0.79"), (0.80, 0.90, "0.80-0.89"), (0.90, 9, "0.90+")):
        b = [t for t, e in ent.items() if lo <= e["p_buy"] < hi]
        closed = [t for t in b if t in ex]; wins = sum(ex[t]["pnl"] > 0 for t in closed)
        rows.append((name, len(b), len(closed), wins, len(b) - len(closed)))
    print("JUDGE CALIBRATION (last 7d)\nbucket      buys  closed  wins  open  winrate")
    for n, b, c, wn, o in rows:
        print(f"{n:<11} {b:>4}  {c:>6}  {wn:>4}  {o:>4}  {(f'{wn/c:.0%}' if c else '-'):>7}")
    rates = [(wn / c) for n, b, c, wn, o in rows if c]
    if len(rates) < 2: print("Verdict: not enough closed trades across buckets to tell whether higher confidence wins more.")
    elif all(rates[i] <= rates[i+1] for i in range(len(rates)-1)): print("Verdict: higher confidence IS winning more so far.")
    else: print("Verdict: higher confidence is NOT winning more. The judge's probabilities are not calibrated.")
    passes = {e["token"]: e for e in L if e["kind"] == "reject" and e.get("seat") == "JUDGE" and e["t"] <= now() - 86400 and e.get("price")}
    toks = list(passes); px = best_pairs(toks) if toks else {}
    green = sum(1 for t in toks if px.get(t, {}).get("priceUsd") and float(px[t]["priceUsd"]) > float(passes[t]["price"]))
    print(f"Passes >=24h old re-priced now: {len(toks)}; would have been green: {green}; dead/unpriced: {sum(1 for t in toks if t not in px)}")
    if not toks: print("(no JUDGE passes older than 24h yet - rerun after a day of cycles)")


def caelan_block():
    try:
        m = mine_load()
        sb = "".join(f"<tr><td>{n}</td><td>${s0:.2f}</td><td>${e:.2f}</td><td>{e/s0-1:+.2%}</td><td>{t}</td><td>{w}/{l}</td><td>{o}</td></tr>" for n, s0, e, t, w, l, o in score_rows(live=False))
        pos = "".join(f"<tr><td>{p['sym']}</td><td>${p['cost']:.2f}</td><td>{p['last']*(1-HAIRCUT)*p['qty']/p['cost']-1:+.1%}</td></tr>" for p in m["positions"].values())
        return (f"<h2>Caelan vs Bots</h2><table><tr><td>who</td><td>start</td><td>equity</td><td>change</td><td>trades</td><td>W/L</td><td>open</td></tr>{sb}</table>"
                f"<h2>Caelan open (manual paper)</h2><table>{pos or '<tr><td>none</td></tr>'}</table>")
    except Exception as ex: return f"<p>caelan block error: {ex}</p>"
def desk_block(p):
    st = load(p); L = read_log(p)[-30:]; eqs = st["equity"][-300:]
    vals = [v for _, v in eqs] or [st["bank_start"]]; lo, hi = min(vals) - 1, max(vals) + 1
    pts = " ".join(f"{i * 600 / max(len(vals) - 1, 1):.0f},{150 - (v - lo) / (hi - lo) * 140:.0f}" for i, v in enumerate(vals))
    rows = "".join(f"<tr><td>{e['ts']}</td><td>{e['kind']}</td><td>{e.get('sym','')}</td><td>{e.get('seat','')}</td>"
                   f"<td>{e.get('reason', e.get('pnl', e.get('size', '')))}</td></tr>" for e in reversed(L) if e["kind"] != "cycle")
    pos = "".join(f"<tr><td>{q['sym']}</td><td>${q['cost']:.2f}</td><td>{q['last']/q['entry']-1:+.1%}</td></tr>" for q in st["positions"].values())
    return (f"<h1>{p['label']} desk</h1><p>bank start ${st['bank_start']:.2f} | equity ${vals[-1]:.2f} | cash ${st['cash']:.2f} | open {len(st['positions'])}</p>"
            f"<svg width=600 height=150 style=\"border:1px solid #131\"><polyline fill=none stroke=\"#3f3\" points=\"{pts}\"/></svg>"
            f"<h2>Open</h2><table>{pos or '<tr><td>none</td></tr>'}</table><h2>Recent activity</h2><table>{rows}</table>")
def _pump_block():
    try: return pump.block()
    except Exception as ex: return f"<p>pump block error: {ex}</p>"
def terminal(st=None):
    html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=60><title>PAPER DESK</title>
<style>body{{background:#000;color:#3f3;font:13px monospace;padding:16px}}td{{padding:2px 8px;border-bottom:1px solid #131}}h1{{font-size:16px}}</style>
<h1>PAPER-ONLY MEMECOIN DESKS - no real money</h1><p>Updated {iso()}</p>{''.join(desk_block(PROFILES[n]) for n in PROFILES)}{_pump_block()}{caelan_block()}"""
    open(os.path.join(HERE, "terminal.html"), "w").write(html)

if __name__ == "__main__":
    args = sys.argv[1:]; cmd = args[0] if args else ""
    desk = args[args.index("--desk") + 1] if "--desk" in args else None
    if desk and desk not in list(PROFILES) + ["pump"]: sys.exit(f"unknown desk {desk}; choose {list(PROFILES) + ['pump']}")
    if cmd == "mine": mine(args[1:])
    elif cmd == "scoreboard": scoreboard()
    elif cmd == "cycle": cycle(desk)
    elif cmd == "recap":
        if desk == "pump": pump.recap()
        elif desk: recap_desk(PROFILES[desk])
        else: recap()
    elif cmd == "calibrate":
        if desk == "pump": print("PUMP has no judge probabilities to calibrate (rules-only desk).")
        for n in ([] if desk == "pump" else [desk] if desk else list(PROFILES)): calibrate(PROFILES[n]); print()
    else: print("usage: desk.py cycle|recap|calibrate [--desk strict|loose|pump] | scoreboard | mine ...")
    if cmd in ("mine", "scoreboard"): terminal()
