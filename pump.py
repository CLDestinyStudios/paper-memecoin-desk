#!/usr/bin/env python3
"""PUMP desk: PAPER-ONLY pump.fun bonding-curve momentum bot. No wallet, no keys, no signing, no paid feeds, never real money.
Data (all free, read-only, no key):
  discovery  pump.fun frontend-api-v3 /coins (newest first); fallback: PumpPortal free websocket subscribeNewToken (~12 s)
  curve      public Solana RPC getMultipleAccounts on bonding-curve accounts -> exact reserves, price, progress, mayhem flag
  flow       DexScreener /tokens/v1 (pumpfun pairs): 5-minute buys/sells
  holders    public Solana RPC getProgramAccounts (token accounts of the mint) -> holders, creator %, top-10, concentration
  launch     public Solana RPC getSignaturesForAddress(curve) -> extra txs in the creation slot (bundle / block-0 snipers)
  creator    frontend-api-v3 /coins?creator=<wallet> (launches in 24h) + RugCheck summary ('Creator history of rugged tokens')
Fills are simulated on the curve formula: 1.25% pump.fun fee + 4% slippage penalty + 0.0005 SOL network fee, each side."""
import json, os, time, base64, struct, threading, datetime as dt
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL = "PUMP"
STATE = os.path.join(HERE, "state_pump.json"); LOG = os.path.join(HERE, "log_pump.jsonl")
CACHE = os.path.join(HERE, "pump_cache.json")    # runner-local (gitignored): RugCheck/creator caches, local creator counts
START_BANK = 500.0
FE = "https://frontend-api-v3.pump.fun/coins"
RPC = "https://api.mainnet-beta.solana.com"
DS = "https://api.dexscreener.com/tokens/v1/solana/"
RC = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
PP_WS = "wss://pumpportal.fun/api/data"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"; TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL_QUOTE = ("11111111111111111111111111111111", "So11111111111111111111111111111111111111112", None, "")
INIT_REAL_TOK = 793_100_000.0     # tokens sold on the curve at 100% (graduation)
SUPPLY = 1_000_000_000.0

R = dict(age_min=120, age_max=600,          # check each coin at 2-10 min old
         # LOOSENED 2026-10-28 ~2:4x AM PT (was: 25 buyers, 2.0x, 15-60%, creator 10%, top10 30%, top5 50%, 0 bundled, 3 launches, 8 deep)
         min_buyers=10,                     # unique buyers (proxy: wallets holding the coin, curve excluded)
         buy_sell_ratio=1.3,                # DexScreener 5m buys >= 1.3x sells
         prog_min=0.05, prog_max=0.75,      # 5-75% of the way to graduation
         creator_max=0.15, top10_max=0.45,  # share of the 1B supply, curve excluded
         top5_most=0.80,                    # skip if the top 5 wallets hold >80% of the tokens outside the curve
         max_bundled=2,                     # skip if more than 2 other buys landed in the creation block
         max_launches_24h=5,                # skip creators with >5 launches in 24h
         size_pct=0.03, max_open=6,
         fee=0.0125, slip=0.04, net_sol=0.0005,
         stop=-0.15, tp1=0.50, tp1_frac=0.5, tp_prog=0.90,   # stops / TP on the curve spot price vs entry spot price
         max_hold_min=45, stall_min=5)
DEADLINE = 35          # seconds; runs alongside STRICT/LOOSE so the 1-minute loop keeps its cadence
MAX_DEEP = 15          # coins per cycle that get the holder / launch / creator checks
RC_BUDGET = 6          # fresh RugCheck calls per cycle (shared IP with the other desks)

S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0 (paper-desk/pump)"
NET = dict(calls=0, http429=0, errors=0, by={}); _lock = threading.Lock()

def now(): return time.time()
def iso(t=None): return dt.datetime.fromtimestamp(t or now()).isoformat(timespec="seconds")
A58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58(b):
    n = int.from_bytes(b, "big"); s = ""
    while n: n, r = divmod(n, 58); s = A58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s

def _count(tag, code=None):
    with _lock:
        NET["calls"] += 1; b = NET["by"].setdefault(tag, [0, 0]); b[0] += 1
        if code == 429: NET["http429"] += 1; b[1] += 1
        if code == "err": NET["errors"] += 1
def get(url, tag, timeout=10):
    try:
        r = S.get(url, timeout=timeout); _count(tag, r.status_code if r.status_code == 429 else None)
        return r.json() if r.status_code == 200 else None
    except Exception: _count(tag, "err"); return None
def rpc(method, params, timeout=15, tries=3):
    for i in range(tries):
        try:
            r = S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
            _count("solana_rpc", r.status_code if r.status_code == 429 else None)
            if r.status_code == 429: time.sleep(1.0 + i); continue
            if r.status_code != 200: return None
            j = r.json()
            if "error" in j and (j["error"] or {}).get("code") in (429, -32429): time.sleep(1.0 + i); continue
            return j.get("result")
        except Exception: _count("solana_rpc", "err"); return None
    return None

# ---------------- state ----------------
def load():
    if os.path.exists(STATE): return json.load(open(STATE))
    return {"desk": LABEL, "paper_only": True, "bank_start": START_BANK, "cash": START_BANK, "positions": {}, "watching": 0, "near": [],
            "traded": [], "equity": [], "cycles": 0, "started": iso(), "sol_usd": None}
def save(st):
    tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w"), indent=1); os.replace(tmp, STATE)
def log(kind, **kw):
    with open(LOG, "a") as f: f.write(json.dumps({"ts": iso(), "t": now(), "kind": kind, "desk": LABEL, **kw}) + "\n")
def read_log(): return [json.loads(l) for l in open(LOG)] if os.path.exists(LOG) else []
def load_cache():
    try: c = json.load(open(CACHE))
    except Exception: c = {}
    for k in ("rc", "cr", "creators", "seen", "ls", "watch"): c.setdefault(k, {})
    c["rc"] = {a: v for a, v in c["rc"].items() if v["t"] > now() - 1800}
    c["cr"] = {a: v for a, v in c["cr"].items() if v["t"] > now() - 600}
    c["creators"] = {a: [t for t in v if t > now() - 86400] for a, v in c["creators"].items()}
    c["creators"] = {a: v for a, v in c["creators"].items() if v}
    c["seen"] = {a: v for a, v in c["seen"].items() if v[0] > now() - 3600}
    c["ls"] = {a: v for a, v in c["ls"].items() if v["last"] > now() - 7200}
    return c
def save_cache(c):
    tmp = CACHE + ".tmp"; json.dump(c, open(tmp, "w")); os.replace(tmp, CACHE)

# ---------------- curve math ----------------
def decode_curve(b64):
    b = base64.b64decode(b64)
    vt, vs, rt, rs, ts = struct.unpack_from("<5Q", b, 8)
    return dict(vt=vt / 1e6, vs=vs / 1e9, rt=rt / 1e6, rs=rs / 1e9, supply=ts / 1e6, complete=bool(b[48]),
                creator=b58(b[49:81]), mayhem=bool(b[81]) if len(b) > 81 else False)
def spot(c): return c["vs"] / c["vt"] if c["vt"] else 0.0                     # SOL per token
def progress(c): return 1.0 if c["complete"] else max(0.0, 1 - c["rt"] / INIT_REAL_TOK)
def quote_buy(c, sol):                                                            # tokens received (paper)
    net = max(sol - R["net_sol"], 0) * (1 - R["fee"])
    return (c["vt"] - c["vs"] * c["vt"] / (c["vs"] + net)) * (1 - R["slip"])
def quote_sell(c, tokens):                                                        # SOL received (paper)
    out = c["vs"] - c["vs"] * c["vt"] / (c["vt"] + tokens)
    return max(out * (1 - R["fee"]) * (1 - R["slip"]) - R["net_sol"], 0.0)
def sell_flat(px_sol, tokens):                                                    # after graduation: PumpSwap price
    return max(tokens * px_sol * (1 - R["fee"]) * (1 - R["slip"]) - R["net_sol"], 0.0)

# ---------------- data ----------------
def discover(st, cache, src):
    """Add new pump.fun launches to the watchlist. Returns the source used."""
    W = cache["watch"]; sol_usd = None; got = 0
    for page in range(6):
        d = get(f"{FE}?offset={page * 50}&limit=50&sort=created_timestamp&order=DESC&includeNsfw=true", "pumpfun_api")
        if not isinstance(d, list):
            if page == 0: return pumpportal(st, cache, src)
            break
        oldest_known = False
        for x in d:
            m = x.get("mint"); ct = (x.get("created_timestamp") or 0) / 1000
            if not m or x.get("complete") or x.get("quote_mint") not in SOL_QUOTE or (x.get("program") or "pump") != "pump": continue
            if not sol_usd and x.get("market_cap") and x.get("usd_market_cap"): sol_usd = x["usd_market_cap"] / x["market_cap"]
            if m in W: oldest_known = True; continue
            if now() - ct > R["age_max"] + 60: continue
            W[m] = [round(ct), (x.get("symbol") or "?")[:16], x.get("creator"), x.get("bonding_curve"), 1 if x.get("token_program") == T22 else 0]
            cache["creators"].setdefault(x.get("creator") or "?", []).append(ct); got += 1
        ages = [now() - (x.get("created_timestamp") or 0) / 1000 for x in d]
        if (page >= 1 and oldest_known) or not ages or max(ages) > R["age_max"] + 30: break
    if sol_usd: st["sol_usd"] = sol_usd
    src["pumpfun_api"] = got; return "pumpfun_api"

def pumpportal(st, cache, src, secs=12):
    """Fallback discovery: PumpPortal free websocket (new-token events only; the trade feed is paid and not used)."""
    try:
        import asyncio, websockets
    except Exception: src["pumpportal"] = "websockets missing"; return "none"
    W = cache["watch"]; got = [0]
    async def run():
        async with websockets.connect(PP_WS, open_timeout=8) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"})); t0 = now()
            while now() - t0 < secs:
                try: m = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                except asyncio.TimeoutError: continue
                if m.get("txType") != "create" or m.get("pool") not in (None, "pump") or m["mint"] in W: continue
                W[m["mint"]] = [round(now()), (m.get("symbol") or "?")[:16], m.get("traderPublicKey"), m.get("bondingCurveKey"), -1]
                cache["creators"].setdefault(m.get("traderPublicKey") or "?", []).append(now()); got[0] += 1
    try: asyncio.run(run()); _count("pumpportal_ws")
    except Exception: _count("pumpportal_ws", "err")
    src["pumpportal"] = got[0]; return "pumpportal"

def curves(bcs):
    out = {}
    for i in range(0, len(bcs), 100):
        chunk = bcs[i:i + 100]
        res = rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])
        for bc, a in zip(chunk, (res or {}).get("value") or []):
            if a and a.get("data"):
                try: out[bc] = decode_curve(a["data"][0])
                except Exception: pass
    return out
def ds_pairs(mints):
    out = {}
    for i in range(0, len(mints), 30):
        for p in get(DS + ",".join(mints[i:i + 30]), "dexscreener") or []:
            a = (p.get("baseToken") or {}).get("address")
            if not a: continue
            if a not in out or ((p.get("liquidity") or {}).get("usd") or 0) > ((out[a].get("liquidity") or {}).get("usd") or 0) or p.get("dexId") == "pumpfun":
                if p.get("dexId") == "pumpfun" or a not in out: out[a] = p
                else: out.setdefault(a + ":best", p)
    return out
def holders(mint, bc, tp):
    for prog in ([T22] if tp == 1 else [TOKEN] if tp == 0 else [T22, TOKEN]):
        res = rpc("getProgramAccounts", [prog, {"encoding": "base64", "dataSlice": {"offset": 32, "length": 40},
                                               "filters": [{"memcmp": {"offset": 0, "bytes": mint}}]}], timeout=15)
        if res is None: return None
        hs = {}
        for a in res:
            b = base64.b64decode(a["account"]["data"][0]); amt = struct.unpack("<Q", b[32:40])[0] / 1e6
            if amt > 0: o = b58(b[:32]); hs[o] = hs.get(o, 0) + amt
        if hs: return hs
    return {}
def launch_slot(bc, cache=None, pages=4):
    """Extra successful txs in the curve's creation slot (bundled / block-0 buys). Cached per curve (it never changes)."""
    if cache is not None and bc in cache["ls"]: return cache["ls"][bc]
    before = None; ok_n = 0; last = None
    for _ in range(pages):
        opt = {"limit": 1000}
        if before: opt["before"] = before
        res = rpc("getSignaturesForAddress", [bc, opt], timeout=15)
        if res is None: return None
        if last is None: last = max([x.get("blockTime") or 0 for x in res] or [0])
        ok_n += sum(1 for x in res if not x.get("err"))
        if len(res) < 1000:
            if not res: return None
            s0 = res[-1]["slot"]
            out = dict(slot0_extra=sum(1 for x in res if x["slot"] == s0 and not x.get("err")) - 1, txs=ok_n, last=last)
            if cache is not None: cache["ls"][bc] = out
            return out
        before = res[-1]["signature"]
    # more than pages*1000 txs: too busy to page back to the creation block cheaply -> unknown, not a reject
    out = dict(slot0_extra=None, txs=ok_n, last=last, too_busy=True)
    if cache is not None: cache["ls"][bc] = out
    return out
def creator_launches(creator, cache):
    c = cache["cr"].get(creator)
    if c: return c["n"]
    d = get(f"{FE}?offset=0&limit=10&sort=created_timestamp&order=DESC&includeNsfw=true&creator={creator}", "pumpfun_api")
    if not isinstance(d, list): return max(len(cache["creators"].get(creator, [])), 0) or None
    n = sum(1 for x in d if x.get("creator") == creator and (x.get("created_timestamp") or 0) / 1000 > now() - 86400)
    n = max(n, len(cache["creators"].get(creator, [])))
    cache["cr"][creator] = {"t": now(), "n": n}; return n
def rugcheck_creator_flag(mint, cache, budget):
    c = cache["rc"].get(mint)
    if c: return c["flag"]
    if budget["left"] <= 0: return None
    budget["left"] -= 1
    try:
        r = S.get(RC.format(mint), timeout=12); _count("rugcheck", r.status_code if r.status_code == 429 else None)
        if r.status_code != 200: return None
        j = r.json(); risks = j.get("risks") or []
        if j.get("rugged"): risks.append({"name": "rugged"})
    except Exception: _count("rugcheck", "err"); return None
    flag = any("creator history" in (x.get("name") or "").lower() or "rugged" in (x.get("name") or "").lower() for x in risks)
    cache["rc"][mint] = {"t": now(), "flag": flag}; return flag

# ---------------- exits ----------------
def value_sol(pos, c, pair, tokens=None):
    tokens = pos["tokens"] if tokens is None else tokens
    if c and not c["complete"] and c["vt"] > 0: return quote_sell(c, tokens), spot(c)
    px = float((pair or {}).get("priceNative") or 0) or pos.get("last_px", 0)
    return sell_flat(px, tokens), px

def close(st, mint, pos, frac, sol, px, reason, sol_usd, extra=None):
    tokens = pos["tokens"] * frac; cost = pos["cost"] * frac
    proceeds = sol * sol_usd; pnl = proceeds - cost      # sol = paper quote for exactly this slice
    st["cash"] += proceeds; pos["tokens"] -= tokens; pos["cost"] -= cost; pos["realized"] = pos.get("realized", 0) + pnl
    full = frac >= 0.999
    if full: (extra := dict(extra or {})).update(trade_pnl=round(pos["realized"], 2))
    log("exit", token=mint, sym=pos["sym"], price=px * sol_usd, price_sol=px, pct=round(frac * 100), partial=not full,
        proceeds=round(proceeds, 2), pnl=round(pnl, 2), ret=round(proceeds / cost - 1, 4) if cost else 0, reason=reason,
        move=round(px / pos["entry_px"] - 1, 4), held_min=round((now() - pos["opened"]) / 60), **(extra or {}))
    if full: del st["positions"][mint]
    return pnl

def manage(st, cvs, pairs, stats, sol_usd):
    for m, pos in list(st["positions"].items()):
        c = cvs.get(pos["bc"]); pair = pairs.get(m)
        if c is None and not pair: stats["stale"] += 1; continue
        sol, px = value_sol(pos, c, pair)
        if not px: stats["stale"] += 1; continue
        pos["last_px"] = px; pos["last_val"] = sol * sol_usd; pos["peak_px"] = max(pos.get("peak_px", px), px)
        prog = progress(c) if c else 1.0; pos["last_prog"] = round(prog, 3)
        move = px / pos["entry_px"] - 1; held = (now() - pos["opened"]) / 60
        b5 = ((pair or {}).get("txns") or {}).get("m5", {}).get("buys") if pair else None
        if b5 is None and held >= R["stall_min"]:
            ls = launch_slot(pos["bc"], pages=1); b5 = 1 if ls and now() - ls["last"] < R["stall_min"] * 60 else 0
        ex = dict(progress=round(prog, 3))
        if move <= R["stop"]: reason = f"stop {move:+.1%} on the curve"
        elif prog >= R["tp_prog"]: reason = f"take profit at {prog:.0%} to graduation" if not (c and c["complete"]) else "graduated (sold on PumpSwap)"
        elif held >= R["max_hold_min"]: reason = f"time stop {R['max_hold_min']} min ({move:+.1%})"
        elif held >= R["stall_min"] and b5 == 0: reason = f"buying stalled 5 min ({move:+.1%})"
        elif move >= R["tp1"] and not pos.get("tp1_done"):
            pos["tp1_done"] = True; stats["exits"] += 1
            part, _ = value_sol(pos, c, pair, pos["tokens"] * R["tp1_frac"])
            pnl = close(st, m, pos, R["tp1_frac"], part, px, f"sold half at {move:+.0%}", sol_usd, ex); stats["win" if pnl > 0 else "loss"] += 1
            pos["last_val"] = value_sol(pos, c, pair)[0] * sol_usd
            continue
        else: continue
        stats["exits"] += 1; pnl = close(st, m, pos, 1.0, sol, px, reason, sol_usd, ex); stats["win" if pnl > 0 else "loss"] += 1

def equity(st, sol_usd):
    return st["cash"] + sum(p.get("last_val", p["cost"]) for p in st["positions"].values())

# ---------------- cycle ----------------
def cycle(deadline=DEADLINE):
    t0 = now(); NET.update(calls=0, http429=0, errors=0, by={})
    st = load(); st["cycles"] += 1; cache = load_cache(); src = {}
    if "watch" in st: cache["watch"].update(st.pop("watch"))   # watchlist lives in the runner-local cache (keeps the committed state small)
    stats = Counter(); stats.update(exits=0, win=0, loss=0, stale=0, entries=0)
    via = discover(st, cache, src)
    sol_usd = st.get("sol_usd") or 0
    W = cache["watch"]
    for m in [m for m, w in W.items() if now() - w[0] > R["age_max"] + 60]: del W[m]
    cand = [m for m, w in W.items() if R["age_min"] <= now() - w[0] <= R["age_max"] and m not in st["traded"]
            and cache["seen"].get(m, [0, ""])[1] not in ("final",)]
    stats["watching"] = len(W); stats["in_window"] = len(cand)
    bcs = list(dict.fromkeys([W[m][3] for m in cand if W[m][3]] + [p["bc"] for p in st["positions"].values()]))
    cvs = curves(bcs) if bcs else {}
    # curve pre-filter
    rej = Counter(); live = []
    def reject(m, why, final=False):
        rej[why.split(";")[0].split("=")[0].split(" ")[0]] += 1
        prev = cache["seen"].get(m, [0, "", ""])
        cache["seen"][m] = [now(), "final" if final else "soft", why]
        return prev
    for m in cand:
        c = cvs.get(W[m][3])
        if c is None: rej["no_curve"] += 1; continue
        if c["mayhem"]: reject(m, "mayhem_mode skip", True); continue
        if c["complete"]: reject(m, "graduated", True); continue
        if abs(c["supply"] - SUPPLY) > 1: reject(m, f"supply {c['supply']:.0f}", True); continue
        pg = progress(c)
        if pg < R["prog_min"]: rej["progress_low"] += 1; continue
        if pg > R["prog_max"]: rej["progress_high"] += 1; continue
        live.append((m, c, pg))
    near = sorted(((progress(cvs[W[m][3]]), m) for m in cand if W[m][3] in cvs and not cvs[W[m][3]]["mayhem"]), reverse=True)[:10]
    stats["progress_ok"] = len(live)
    pairs = ds_pairs(list(dict.fromkeys([m for m, _, _ in live] + list(st["positions"])))) if (live or st["positions"]) else {}
    if not sol_usd:
        for p in pairs.values():
            if p.get("priceUsd") and p.get("priceNative"): sol_usd = float(p["priceUsd"]) / float(p["priceNative"]); st["sol_usd"] = sol_usd; break
    if sol_usd: manage(st, cvs, pairs, stats, sol_usd)
    deep = []
    for m, c, pg in live:
        tx = ((pairs.get(m) or {}).get("txns") or {}).get("m5") or {}
        b, s = tx.get("buys"), tx.get("sells")
        if b is None: rej["no_dex_data"] += 1; continue
        if b < 1 or b < R["buy_sell_ratio"] * (s or 0): rej["buy_sell_ratio"] += 1; continue
        deep.append((m, c, pg, b, s or 0))
    # coins deep-checked in the last 90 s go to the back so the 15 slots rotate through the candidates
    deep.sort(key=lambda x: (now() - (cache["seen"].get(x[0]) or [0])[0] < 90, -x[2])); deep = deep[:MAX_DEEP]; stats["deep_checked"] = len(deep)
    budget = {"left": RC_BUDGET}
    def deep_one(item):
        m, c, pg, b, s = item; w = W[m]
        return item, holders(m, w[3], w[4]), launch_slot(w[3], cache)
    results = []
    if deep and now() - t0 < deadline:
        with ThreadPoolExecutor(3) as ex: results = list(ex.map(deep_one, deep))
    for (m, c, pg, b, s), hs, ls in results:
        w = W[m]; sym, creator, bc = w[1], w[2], w[3]
        if now() - t0 > deadline + 10: break
        if hs is None: rej["holders_missing"] += 1; continue
        hs = {o: a for o, a in hs.items() if o != bc}
        circ = sum(hs.values()); top = sorted(hs.values(), reverse=True)
        f = dict(progress=round(pg, 3), buys5=b, sells5=s, holders=len(hs), creator_pct=round(hs.get(creator, 0) / SUPPLY, 4),
                 top10_pct=round(sum(top[:10]) / SUPPLY, 4), top5_circ=round(sum(top[:5]) / circ, 3) if circ else 1.0,
                 slot0_extra=(ls or {}).get("slot0_extra"), txs=(ls or {}).get("txs"))
        fails = []
        if f["holders"] < R["min_buyers"]: fails.append(f"buyers={f['holders']}")
        if f["creator_pct"] > R["creator_max"]: fails.append(f"creator={f['creator_pct']:.1%}")
        if f["top10_pct"] > R["top10_max"]: fails.append(f"top10={f['top10_pct']:.1%}")
        if f["top5_circ"] > R["top5_most"]: fails.append(f"top5_hold_most={f['top5_circ']:.0%}")
        final = False
        if ls is None: fails.append("launch_slot=missing")
        elif ls["slot0_extra"] is None: f["launch_slot"] = "unknown (>4000 txs)"
        elif ls["slot0_extra"] > R["max_bundled"]: fails.append(f"bundled_launch={ls['slot0_extra']} extra buys in creation block"); final = True
        if not fails:
            n = creator_launches(creator, cache); f["creator_launches_24h"] = n
            if n is None: fails.append("creator_launches=missing")
            elif n > R["max_launches_24h"]: fails.append(f"serial_creator={n} launches/24h"); final = True
        if not fails:
            flag = rugcheck_creator_flag(m, cache, budget); f["rugcheck_creator_flag"] = flag
            if flag is None: fails.append("rugcheck=missing")
            elif flag: fails.append("rugcheck=creator history of rugs"); final = True
        if fails:
            why = "; ".join(fails); prev = reject(m, why, final)
            if prev[2] != why: log("reject", token=m, sym=sym, reason=why, facts=f)
            continue
        if len(st["positions"]) >= R["max_open"]: rej["max_open"] += 1; continue
        if not sol_usd: rej["no_sol_price"] += 1; continue
        size = round(min(R["size_pct"] * equity(st, sol_usd), st["cash"]), 2)
        if size < 1: rej["no_cash"] += 1; continue
        tokens = quote_buy(c, size / sol_usd); px = spot(c)
        st["cash"] -= size; st["traded"].append(m); cache["seen"][m] = [now(), "final", "bought"]
        st["positions"][m] = dict(sym=sym, bc=bc, tokens=tokens, cost=size, cost0=size, entry_px=px, entry_fill=size / sol_usd / tokens,
                                  opened=now(), last_px=px, peak_px=px, last_val=quote_sell(c, tokens) * sol_usd, last_prog=round(pg, 3))
        stats["entries"] += 1
        log("entry", token=m, sym=sym, price=px * sol_usd, price_sol=px, size=size, tokens=tokens, sol_usd=round(sol_usd, 2), facts=f,
            mark_value=round(st["positions"][m]["last_val"], 2))
    st["traded"] = st["traded"][-500:]; st["watching"] = len(W)
    st["near"] = [[W[m][1], m, round(pg, 3), round((now() - W[m][0]) / 60, 1), (cache["seen"].get(m) or [0, "", ""])[2] if len(cache["seen"].get(m) or []) > 2 else ""]
                  for pg, m in near]          # top-10 coins in the 2-10 min window by curve progress: [sym, mint, progress, age min, last deep-check reason]
    save_cache(cache)
    eq = round(equity(st, sol_usd), 2); E = st["equity"]
    if E and len(E) > 1 and E[-1][1] == eq and E[-2][1] == eq and now() - dt.datetime.fromisoformat(E[-2][0]).timestamp() < 600: E[-1] = [iso(), eq]
    else: E.append([iso(), eq])
    st["equity"] = E[-3000:]; save(st)
    secs = round(now() - t0, 1)
    net = dict(calls=NET["calls"], http429=NET["http429"], errors=NET["errors"], by=NET["by"])
    stats = dict(stats); log("cycle", stats=stats, rejects=dict(rej), via=via, src=src, net=net, equity=eq, cash=round(st["cash"], 2),
                             open=len(st["positions"]), secs=secs, sol_usd=round(sol_usd or 0, 2))
    return (f"CYCLE PUMP {st['cycles']} {iso()} | via {via} new {sum(v for v in src.values() if isinstance(v, int))} | watching {len(W)} "
            f"| 2-10min {stats['in_window']} | {R['prog_min']:.0%}-{R['prog_max']:.0%} curve {stats['progress_ok']} | deep {stats['deep_checked']} | rejects {dict(rej)} "
            f"| entries {stats['entries']} exits {stats['exits']} (W{stats['win']}/L{stats['loss']}) | open {len(st['positions'])} | equity ${eq:.2f} "
            f"| http {NET['calls']} calls, 429s {NET['http429']} {({k: v[1] for k, v in NET['by'].items() if v[1]})}, errors {NET['errors']} | {secs:.0f}s")

# ---------------- reporting ----------------
def row():
    st = load(); L = read_log(); ex = [e for e in L if e["kind"] == "exit" and not e.get("partial")]   # W/L per closed trade
    eq = st["equity"][-1][1] if st["equity"] else st["bank_start"]
    return ("Pump", st["bank_start"], eq, sum(e["kind"] == "entry" for e in L), sum(e.get("trade_pnl", e["pnl"]) > 0 for e in ex),
            sum(e.get("trade_pnl", e["pnl"]) <= 0 for e in ex), len(st["positions"]))

def recap():
    st = load(); L = read_log(); since = now() - 86400; Rr = [e for e in L if e["t"] >= since]
    cyc = [e for e in Rr if e["kind"] == "cycle"]; ent = [e for e in Rr if e["kind"] == "entry"]; ex = [e for e in Rr if e["kind"] == "exit"]
    rk = [e for e in Rr if e["kind"] == "reject"]
    rej = Counter()
    for e in cyc: rej.update(e.get("rejects") or {})
    done = [e for e in ex if not e.get("partial")]; w = sum(e.get("trade_pnl", e["pnl"]) > 0 for e in done); pnl = sum(e["pnl"] for e in ex)
    eq = st["equity"][-1][1] if st["equity"] else st["bank_start"]
    seen = sum((e.get("stats") or {}).get("in_window", 0) for e in cyc)
    via = Counter(e.get("via") for e in cyc)
    print(f"== PUMP DESK (state_pump.json) ==")
    print(f"DESK PUMP {dt.date.today()} / cycles {len(cyc)} (data via {dict(via)}) / coin checks at 2-10 min {seen} / deep-checked rejects {len(rk)} "
          f"/ trades executed {len(ent)} sells {len(ex)} closed W/L {w}/{len(done) - w} (realized ${pnl:+.2f}) / bank ${st['bank_start']:.2f}->${eq:.2f} ({eq / st['bank_start'] - 1:+.2%})")
    for e in ent: print(f"  BUY  {e['sym']} ${e['size']:.2f} @ ${e['price']:.10f} (curve {e['facts']['progress']:.0%}, {e['facts']['holders']} holders) ({e['ts']})")
    for e in ex: print(f"  SELL {e['sym']} {e.get('pct', 100)}% P&L ${e['pnl']:+.2f} ({e['ret']*100:+.1f}%) {e['reason']} ({e['ts']})")
    for m, p in st["positions"].items():
        print(f"  OPEN {p['sym']} cost ${p['cost']:.2f} value ${p.get('last_val', p['cost']):.2f} ({p.get('last_val', p['cost']) / p['cost'] - 1:+.1%}) curve {p.get('last_prog', 0):.0%}")
    print(f"  (open positions: {len(st['positions'])}, cash ${st['cash']:.2f}; skip reasons 24h: {dict(rej.most_common(8))})")

def block():
    st = load(); L = read_log()[-30:]; vals = [v for _, v in st["equity"][-300:]] or [st["bank_start"]]
    lo, hi = min(vals) - 1, max(vals) + 1
    pts = " ".join(f"{i * 600 / max(len(vals) - 1, 1):.0f},{150 - (v - lo) / (hi - lo) * 140:.0f}" for i, v in enumerate(vals))
    rows = "".join(f"<tr><td>{e['ts']}</td><td>{e['kind']}</td><td>{e.get('sym','')}</td><td>{e.get('reason', e.get('pnl', e.get('size', '')))}</td></tr>"
                   for e in reversed(L) if e["kind"] != "cycle")
    pos = "".join(f"<tr><td>{q['sym']}</td><td>${q['cost']:.2f}</td><td>{q.get('last_val', q['cost']) / q['cost'] - 1:+.1%}</td></tr>" for q in st["positions"].values())
    return (f"<h1>PUMP desk (pump.fun curve, paper)</h1><p>bank start ${st['bank_start']:.2f} | equity ${vals[-1]:.2f} | cash ${st['cash']:.2f} | open {len(st['positions'])} | watching {st.get('watching', 0)}</p>"
            f"<svg width=600 height=150 style=\"border:1px solid #131\"><polyline fill=none stroke=\"#3f3\" points=\"{pts}\"/></svg>"
            f"<h2>Open</h2><table>{pos or '<tr><td>none</td></tr>'}</table><h2>Recent activity</h2><table>{rows}</table>")

if __name__ == "__main__":
    print(cycle())
