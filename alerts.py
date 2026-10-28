#!/usr/bin/env python3
"""Print paper-desk buys/sells since the last alert run, then advance the cursor. Paper only."""
import json, os, sys
D = os.path.dirname(os.path.abspath(__file__))
CUR = os.path.join(D, "alert_cursor.json")
peek = "--peek" in sys.argv
last = json.load(open(CUR))["t"] if os.path.exists(CUR) else None
events, eq, newest = [], None, last or 0
for line in open(os.path.join(D, "log.jsonl")):
    try: e = json.loads(line)
    except Exception: continue
    t = e.get("t", 0)
    if e.get("kind") == "cycle": eq = e.get("equity")
    if e.get("kind") in ("entry", "exit"):
        newest = max(newest, t)
        if last is None or t > last: events.append(e)
if last is None and not peek:  # first run: start fresh, don't replay history
    json.dump({"t": newest}, open(CUR, "w")); print("CURSOR_INITIALIZED (no alerts)"); sys.exit()
if not events: print("NO_NEW_TRADES"); sys.exit()
for e in events:
    link = f"https://dexscreener.com/solana/{e['token']}"
    if e["kind"] == "entry":
        print(f"BUY {e['sym']} at ${e['price']} | paper size ${e['size']:.2f} (x5 = ${e['size']*5:.2f}) | judge confidence {e.get('p_buy')} | contract {e['token']} | {link} | {e['ts']}")
    else:
        print(f"SELL {e['sym']} at ${e['price']} | P&L ${e['pnl']:+.2f} (x5 = ${e['pnl']*5:+.2f}), {e['ret']*100:+.1f}% | reason: {e.get('reason')} | {link} | {e['ts']}")
if eq is not None: print(f"BANK ${eq:.2f} on $500 ({(eq-500)/5:+.1f}%) | x5 = ${eq*5:.2f} on $2,500")
if not peek: json.dump({"t": max(e['t'] for e in events)}, open(CUR, "w"))
