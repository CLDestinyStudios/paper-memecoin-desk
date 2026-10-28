#!/usr/bin/env python3
"""PAPER-ONLY Solana memecoin desk. No wallets, no keys, no signing, no real money.
Read-only public APIs: DexScreener, RugCheck. Thresholds copied from @x_insider4 (experiment)."""
import json, os, sys, time, datetime as dt
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "log.jsonl")
DS = "https://api.dexscreener.com"
RC = "https://api.rugcheck.xyz/v1/tokens/{}/report"
START_BANK = 500.0
MAX_POS_PCT = 0.08          # never more than 8% of current paper balance
HAIRCUT = 0.02              # slippage+fee per side
STOP = -0.06                # hard stop
TRAIL_ARM = 0.12            # trail only after +12%
TRAIL_GIVEBACK = 0.10       # trail distance from peak (my choice; post doesn't specify)
MAX_CANDIDATES = 60         # per cycle, keeps run < 2 min
DEADLINE = 100              # seconds
S = requests.Session(); S.headers["User-Agent"] = "paper-desk/1.0"

def now(): return time.time()
def iso(t=None): return dt.datetime.fromtimestamp(t or now()).isoformat(timespec="seconds")

def load():
    if os.path.exists(STATE): return json.load(open(STATE))
    return {"bank_start": START_BANK, "cash": START_BANK, "positions": {}, "seen": {}, "watch": {},
            "traded": [], "equity": [], "cycles": 0}
def save(st):
    tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w"), indent=1); os.replace(tmp, STATE)
def log(kind, **kw):
    with open(LOG, "a") as f: f.write(json.dumps({"ts": iso(), "t": now(), "kind": kind, **kw}) + "\n")
def get(url, timeout=12):
    try:
        r = S.get(url, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception: return None

# ---------------- SEAT 1: SCOUT ----------------
def scout():
    addrs = []
    for ep in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        for x in get(DS + ep) or []:
            if x.get("chainId") == "solana": addrs.append(x["tokenAddress"])
    for q in ("pumpswap", "raydium", "pump"):
        for p in (get(f"{DS}/latest/dex/search?q={q}") or {}).get("pairs") or []:
            if p.get("chainId") == "solana" and p.get("pairCreatedAt", 0) > (now() - 3 * 86400) * 1000:
                addrs.append(p["baseToken"]["address"])
    return list(dict.fromkeys(addrs))

def best_pairs(addrs):
    """Map token -> its most liquid Solana pair (DexScreener /tokens/v1, 30 per call)."""
    out = {}
    for i in range(0, len(addrs), 30):
        for p in get(f"{DS}/tokens/v1/solana/{','.join(addrs[i:i+30])}") or []:
            a = p["baseToken"]["address"]
            if a in addrs and (a not in out or (p.get("liquidity") or {}).get("usd", 0) > (out[a].get("liquidity") or {}).get("usd", 0)):
                out[a] = p
    return out

# ---------------- SEAT 2: FILTER / RISK ----------------
BAD_RISK_WORDS = ("rug", "creator history", "dev", "insider", "honeypot", "freeze", "copycat", "scam")
def risk(addr, pair):
    rc = get(RC.format(addr), timeout=15)
    f = {}
    if rc is None:
        f = dict(mint_revoked="missing", lp_locked="missing", top10_pct="missing", bad_dev="missing")
    else:
        f["mint_revoked"] = rc.get("mintAuthority") is None
        f["freeze_revoked"] = rc.get("freezeAuthority") is None
        mk = [m for m in rc.get("markets") or [] if m.get("pubkey") == pair.get("pairAddress")] or (rc.get("markets") or [])
        lp = (mk[0].get("lp") or {}) if mk else {}
        f["lp_locked_pct"] = lp.get("lpLockedPct", "missing") if mk else "missing"
        f["lp_locked"] = "missing" if f["lp_locked_pct"] == "missing" else f["lp_locked_pct"] >= 95
        skip = {k for k, v in (rc.get("knownAccounts") or {}).items() if v.get("type") in ("AMM", "LOCKER")}
        skip |= {m.get("pubkey") for m in rc.get("markets") or []}
        th = rc.get("topHolders")
        if th is None: f["top10_pct"] = "missing"
        else:
            hs = [h for h in th if h.get("owner") not in skip and h.get("address") not in skip][:10]
            f["top10_pct"] = round(sum(h.get("pct", 0) for h in hs), 2)
        names = [r.get("name", "") for r in rc.get("risks") or [] if r.get("level") == "danger"]
        f["danger_flags"] = names
        f["bad_dev"] = bool(rc.get("rugged")) or any(w in n.lower() for n in names for w in BAD_RISK_WORDS)
        f["insiders_detected"] = rc.get("graphInsidersDetected", "missing")
        f["holders"] = rc.get("totalHolders", "missing")
        f["rc_score"] = rc.get("score_normalised", "missing")
    f["vol_h1"] = ((pair.get("volume") or {}).get("h1"))
    if f["vol_h1"] is None: f["vol_h1"] = "missing"
    fails = []
    def chk(name, val, ok):
        if val == "missing": fails.append(f"{name}=missing")
        elif not ok(val): fails.append(f"{name}={val}")
    chk("mint_revoked", f["mint_revoked"], lambda v: v is True)
    chk("lp_locked", f["lp_locked"], lambda v: v is True)
    chk("top10_pct", f["top10_pct"], lambda v: v <= 20)
    chk("vol_h1", f["vol_h1"], lambda v: v >= 50_000)
    chk("bad_dev", f["bad_dev"], lambda v: v is False)
    return f, fails   # one hard fail = dead

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

# ---------------- SEAT 4: JUDGE (7th seat; deterministic, closed answers only) ----------------
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
def judge(d):
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
    a["gate"] = (action == "buy" and p_buy >= 0.70 and a["organic"] == "yes" and p_org >= 0.65 and not late and exit_risk <= 3)
    return a

# ---------------- SEAT 5: EXECUTOR (paper) ----------------
def equity(st, prices):
    return st["cash"] + sum(p["qty"] * prices.get(k, p["last"]) * (1 - HAIRCUT) for k, p in st["positions"].items())
def paper_buy(st, addr, d, j):
    price = float(d["price_usd"]); bal = equity(st, {})
    size = round(min(MAX_POS_PCT * bal, st["cash"]), 2)       # confidence never changes size
    if size < 1: return log("reject", token=addr, seat="EXECUTOR", reason="no cash")
    fill = price * (1 + HAIRCUT); qty = size / fill
    st["cash"] -= size; st["traded"].append(addr)
    st["positions"][addr] = {"sym": d["symbol"], "qty": qty, "entry": fill, "mark_entry": price, "cost": size,
                             "peak": price, "last": price, "opened": now(), "p_buy": j["p_buy"]}
    log("entry", token=addr, sym=d["symbol"], price=price, fill=fill, size=size, p_buy=j["p_buy"], judge=j)

# ---------------- SEAT 6: RISK / EXIT ----------------
def manage(st, stats):
    if not st["positions"]: return {}
    pairs = best_pairs(list(st["positions"]))
    prices = {}
    for a, p in list(st["positions"].items()):
        pr = pairs.get(a, {}).get("priceUsd")
        if pr is None: stats["stale"] += 1; continue
        pr = float(pr); prices[a] = pr; p["last"] = pr; p["peak"] = max(p["peak"], pr)
        exit_px = pr * (1 - HAIRCUT); ret = exit_px / p["entry"] - 1
        peak_ret = p["peak"] / p["mark_entry"] - 1
        reason = None
        if ret <= STOP: reason = f"hard stop {ret:+.1%}"
        elif peak_ret >= TRAIL_ARM and pr <= p["peak"] * (1 - TRAIL_GIVEBACK): reason = f"trail hit (peak {peak_ret:+.1%})"
        if reason:
            proceeds = p["qty"] * exit_px; pnl = proceeds - p["cost"]
            st["cash"] += proceeds; del st["positions"][a]
            stats["exits"] += 1; stats["win" if pnl > 0 else "loss"] += 1
            log("exit", token=a, sym=p["sym"], price=pr, fill=exit_px, pnl=round(pnl, 2), ret=round(ret, 4), reason=reason, p_buy=p["p_buy"])
    return prices

# ---------------- CYCLE ----------------
def cycle():
    t0 = now(); st = load(); st["cycles"] += 1
    stats = dict(leads=0, risk_killed=0, analyst_killed=0, buy=0, watch=0, pass_=0, clean_pass=0, entries=0, exits=0, win=0, loss=0, stale=0)
    prices = manage(st, stats)
    lost_this_cycle = stats["loss"] > 0         # no revenge trades: no new entries in a cycle that took a loss
    addrs = scout()
    todo = [a for a in addrs if a in st["watch"]] + [a for a in addrs if a not in st["seen"] and a not in st["positions"]]
    todo += [a for a in st["watch"] if a not in todo]
    todo = todo[:MAX_CANDIDATES]
    pairs = best_pairs(todo)
    for a in todo:
        if now() - t0 > DEADLINE: break
        pair = pairs.get(a)
        if not pair or pair.get("chainId") != "solana": st["seen"][a] = now(); continue
        stats["leads"] += 1; st["seen"][a] = now()
        sym = pair["baseToken"].get("symbol"); price = pair.get("priceUsd")
        f, fails = risk(a, pair); time.sleep(0.3)
        if fails:
            stats["risk_killed"] += 1; st["watch"].pop(a, None)
            log("reject", token=a, sym=sym, seat="RISK", reason="; ".join(fails), facts=f, price=price); continue
        score, parts = analyst(pair, f)
        if score < 0.6:
            stats["analyst_killed"] += 1; st["watch"].pop(a, None)
            log("reject", token=a, sym=sym, seat="ANALYST", reason=f"score {score} < 0.6", parts=parts, price=price); continue
        d = dossier(a, pair, f); j = judge(d)
        if j["gate"] and a not in st["traded"] and not lost_this_cycle and a not in st["positions"]:
            stats["buy"] += 1; st["watch"].pop(a, None); paper_buy(st, a, d, j); stats["entries"] += 1; continue
        if j["action"] == "watch" and a not in st["watch"]:
            stats["watch"] += 1; st["watch"][a] = now()
            log("watch", token=a, sym=sym, judge=j, dossier=d, price=price); continue
        why = "second watch -> pass" if a in st["watch"] else ("gate not met" if not j["gate"] else "no re-entry / revenge guard")
        st["watch"].pop(a, None); stats["pass_"] += 1; stats["clean_pass"] += 1
        log("reject", token=a, sym=sym, seat="JUDGE", reason=why, judge=j, dossier=d, price=price, risk_clean=True)
    eq = round(equity(st, prices), 2)
    st["equity"].append([iso(), eq]); save(st)
    log("cycle", stats=stats, equity=eq, cash=round(st["cash"], 2), open=len(st["positions"]), secs=round(now() - t0, 1))
    terminal(st)
    print(f"CYCLE {st['cycles']} {iso()} | leads {stats['leads']} | RISK killed {stats['risk_killed']} | ANALYST killed {stats['analyst_killed']} "
          f"| JUDGE buy/watch/pass {stats['buy']}/{stats['watch']}/{stats['pass_']} | exits {stats['exits']} (W{stats['win']}/L{stats['loss']}) "
          f"| open {len(st['positions'])} | equity ${eq:.2f} | {now()-t0:.0f}s")


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
            ("Bot", st["bank_start"], b_eq, sum(e["kind"] == "entry" for e in L), sum(e["pnl"] > 0 for e in ex), sum(e["pnl"] <= 0 for e in ex), len(st["positions"]))]
def scoreboard(live=True):
    print("CAELAN vs BOT (paper)\nwho      start     equity    change   trades  W/L   open")
    for n, s0, e, t, w, l, o in score_rows(live):
        print(f"{n:<8} ${s0:<8.2f} ${e:<8.2f} {e / s0 - 1:+7.2%}  {t:>5}   {w}/{l}   {o}")
    print("(Caelan trades = closed sells; Bot trades = entries. Equity marked at live price minus 2% exit haircut.)")

# ---------------- SEAT 7: LOGGER readers ----------------
def read_log():
    return [json.loads(l) for l in open(LOG)] if os.path.exists(LOG) else []

def recap():
    st = load(); L = read_log(); since = now() - 86400
    R = [e for e in L if e["t"] >= since]
    rej = [e for e in R if e["kind"] == "reject"]
    rk = [e for e in rej if e.get("seat") == "RISK"]
    jp = [e for e in rej if e.get("seat") == "JUDGE"]
    ent = [e for e in R if e["kind"] == "entry"]; ex = [e for e in R if e["kind"] == "exit"]
    w = sum(e["pnl"] > 0 for e in ex); l = len(ex) - w
    leads = sum(e["stats"]["leads"] for e in R if e["kind"] == "cycle")
    watch = sum(e["kind"] == "watch" for e in R)
    eq = st["equity"][-1][1] if st["equity"] else st["bank_start"]
    from collections import Counter
    c = Counter(r.split("=")[0] for e in rk for r in e["reason"].split("; "))
    worked = (f"RISK gate held: {len(rk)} of {leads} leads killed before any money moved (top reason: {c.most_common(1)[0][0]})" if c
              else "No trades forced; desk stayed in cash")
    if ex: worked = f"{w}/{len(ex)} closed trades green; stops kept losers small" if w else worked
    missing = sum("missing" in e["reason"] for e in rk)
    change = (f"{missing} leads died on 'missing' data - consider a 2nd data source" if missing > len(rk) / 3
              else f"Most kills are '{c.most_common(1)[0][0]}' - scout is surfacing mostly un-tradeable pairs; tighten scout pre-filter" if c
              else "Need more cycles before changing anything")
    print(f"DESK {dt.date.today()} / leads found {leads} / killed by RISK {len(rk)} / JUDGE buy/watch/pass {len(ent)}/{watch}/{len(jp)} "
          f"/ passed-but-RISK-clean {len(jp)} / trades executed {len(ent)} W/L {w}/{l} / bank ${st['bank_start']:.2f}->${eq:.2f} "
          f"/ worked: {worked} / change: {change}")
    print(f"  (open positions: {len(st['positions'])}, cash ${st['cash']:.2f}; RISK kill reasons: {dict(c)})")
    print(); scoreboard()

def calibrate():
    L = read_log(); since = now() - 7 * 86400
    ent = {e["token"]: e for e in L if e["kind"] == "entry" and e["t"] >= since}
    ex = {e["token"]: e for e in L if e["kind"] == "exit"}
    st = load()
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
        return (f"<h2>Caelan vs Bot</h2><table><tr><td>who</td><td>start</td><td>equity</td><td>change</td><td>trades</td><td>W/L</td><td>open</td></tr>{sb}</table>"
                f"<h2>Caelan open (manual paper)</h2><table>{pos or '<tr><td>none</td></tr>'}</table>")
    except Exception as ex: return f"<p>caelan block error: {ex}</p>"
def terminal(st):
    L = read_log()[-40:]; eqs = st["equity"][-200:]
    vals = [v for _, v in eqs] or [st["bank_start"]]; lo, hi = min(vals) - 1, max(vals) + 1
    pts = " ".join(f"{i * 600 / max(len(vals) - 1, 1):.0f},{150 - (v - lo) / (hi - lo) * 140:.0f}" for i, v in enumerate(vals))
    rows = "".join(f"<tr><td>{e['ts']}</td><td>{e['kind']}</td><td>{e.get('sym','')}</td><td>{e.get('seat','')}</td>"
                   f"<td>{e.get('reason', e.get('pnl', e.get('size', '')))}</td></tr>" for e in reversed(L) if e["kind"] != "cycle")
    pos = "".join(f"<tr><td>{p['sym']}</td><td>${p['cost']:.2f}</td><td>{p['last']/p['entry']-1:+.1%}</td></tr>" for p in st["positions"].values())
    html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=60><title>PAPER DESK</title>
<style>body{{background:#000;color:#3f3;font:13px monospace;padding:16px}}td{{padding:2px 8px;border-bottom:1px solid #131}}h1{{font-size:16px}}</style>
<h1>PAPER-ONLY MEMECOIN DESK - no real money</h1><p>Updated {iso()} | bank start ${st['bank_start']:.2f} | equity ${vals[-1]:.2f} | cash ${st['cash']:.2f} | open {len(st['positions'])}</p>
<svg width=600 height=150 style="border:1px solid #131"><polyline fill=none stroke="#3f3" points="{pts}"/></svg>
<h2>Open</h2><table>{pos or '<tr><td>none</td></tr>'}</table>{caelan_block()}<h2>Recent activity</h2><table>{rows}</table>"""
    open(os.path.join(HERE, "terminal.html"), "w").write(html)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mine": mine(sys.argv[2:])
    elif cmd == "scoreboard": scoreboard()
    else: {"cycle": cycle, "recap": recap, "calibrate": calibrate}.get(cmd, lambda: print("usage: desk.py cycle|recap|calibrate|scoreboard|mine ..."))()
    if cmd in ("mine", "scoreboard") and os.path.exists(STATE): terminal(load())
