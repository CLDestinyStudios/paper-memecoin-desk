#!/bin/bash
# Paper-only desk loop: one cycle every 5 minutes. No LLM, no money.
D="$(cd "$(dirname "$0")" && pwd)"
while true; do
  echo "=== $(date) ===" >> "$D/loop.log"
  timeout 300 python3 "$D/desk.py" cycle >> "$D/loop.log" 2>&1
  sleep 300
done
