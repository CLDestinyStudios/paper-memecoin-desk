#!/usr/bin/env python3
"""Print paper-desk buys/sells (STRICT + LOOSE + PUMP desks) since the last alert run, then advance the cursors. Paper only.
Cursor file alert_cursor.json (box-local, gitignored): {"strict": <t>, "loose": <t>, "pump": <t>}. A legacy {"t": x} is read as the strict cursor.
First run with no strict cursor: CURSOR_INITIALIZED (no replay). The loose and pump cursors start at 0 so their first trades ping.
--peek prints without advancing cursors."""
import json, os, sys
D = os.path.dirname(os.path.abspath(__file__))
CUR = os.path.join(D, "alert_cursor.json")
DESKS = (("strict", "STRICT", "log.jsonl"), ("loose", "LOOSE", "log_loose.jsonl"), ("pump", "PUMP", "log_pump.jsonl"))
peek = "--peek" in sys.argv
cur = json.load(open(CUR)) if os.path.exists(CUR) else None
if cur is not None and "t" in cur and "strict" not in cur: cur = {"strict": cur["t"]}      # legacy single-desk cursor
if cur is not None: cur.setdefault("loose", 0); cur.setdefault("pump", 0)

def scan(fname, last):
    events, eq, newest = [], None, last or 0
    path = os.path.join(D, fname)
    if not os.path.exists(path): return events, eq, newest
    for line in open(path):
        try: e = json.loads(line)
        except Exception: continue
        t = e.get("t", 0)
        if e.get("kind") == "cycle": eq = e.get("equity")
        if e.get("kind") in ("entry", "exit"):
            newest = max(newest, t)
            if last is None or t > last: events.append(e)
    return events, eq, newest

res = {k: scan(f, (cur or {}).get(k)) for k, _, f in DESKS}
if cur is None and not peek:   # first run ever: strict starts fresh (no replay); loose starts at 0 so its first trades ping
    new = {"strict": res["strict"][2], "loose": 0, "pump": 0}
    json.dump(new, open(CUR, "w")); print("CURSOR_INITIALIZED (no alerts)"); sys.exit()
if not any(res[k][0] for k, _, _ in DESKS): print("NO_NEW_TRADES"); sys.exit()
for k, tag, _ in DESKS:
    for e in res[k][0]:
        link = f"https://dexscreener.com/solana/{e['token']}"
        if tag == "PUMP":
            f = e.get("facts") or {}; pf = f"https://pump.fun/coin/{e['token']}"
            if e["kind"] == "entry":
                print(f"PUMP BUY {e['sym']} at ${e['price']:.10f} (mcap ${e['price']*1e9:,.0f}) | paper size ${e['size']:.2f} (x5 = ${e['size']*5:.2f}) | curve {f.get('progress', 0)*100:.0f}% to graduation, "
                      f"{f.get('holders')} holders, 5m buys/sells {f.get('buys5')}/{f.get('sells5')} | contract {e['token']} | {pf} | {e['ts']}")
            else:
                part = f" {e.get('pct', 100)}%" if e.get("partial") else ""
                print(f"PUMP SELL{part} {e['sym']} at ${e['price']:.10f} (mcap ${e['price']*1e9:,.0f}) | P&L ${e['pnl']:+.2f} (x5 = ${e['pnl']*5:+.2f}), {e['ret']*100:+.1f}% | reason: {e.get('reason')} | {pf} | {e['ts']}")
            continue
        if e["kind"] == "entry":
            print(f"{tag} BUY {e['sym']} at ${e['price']} | paper size ${e['size']:.2f} (x5 = ${e['size']*5:.2f}) | judge confidence {e.get('p_buy')} | contract {e['token']} | {link} | {e['ts']}")
        else:
            print(f"{tag} SELL {e['sym']} at ${e['price']} | P&L ${e['pnl']:+.2f} (x5 = ${e['pnl']*5:+.2f}), {e['ret']*100:+.1f}% | reason: {e.get('reason')} | {link} | {e['ts']}")
for k, tag, _ in DESKS:
    eq = res[k][1]
    if eq is not None: print(f"{tag} BANK ${eq:.2f} on $500 ({(eq-500)/5:+.1f}%) | x5 = ${eq*5:.2f} on $2,500")
if not peek:
    new = dict(cur)
    for k, _, _ in DESKS:
        if res[k][0]: new[k] = max(e["t"] for e in res[k][0])
    json.dump(new, open(CUR, "w"))
