#!/usr/bin/env bash
# Read-only status of the live-ready overnight paper soak.
# Does not signal any process. Never touches pid 129703.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOAK="$ROOT/soak_overnight"
LOCK="$ROOT/RUNNING.lock"
HB="$SOAK/state/heartbeat.json"
STATUS="$SOAK/state/status.json"
ALERTS="$SOAK/logs/alerts.jsonl"
PROTECTED=129703

alive() {
  local pid="${1:-}"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ "$pid" != "$PROTECTED" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

sup_pid=""
run_pid=""
if [[ -f "$LOCK" ]]; then
  sup_pid="$(awk -F': *' '/^pid:/{print $2; exit}' "$LOCK" | tr -d ' ')"
  run_pid="$(awk -F': *' '/^child_pid:/{print $2; exit}' "$LOCK" | tr -d ' ')"
fi
if [[ -f "$HB" ]]; then
  hb_child="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('child_pid') or json.load(open(sys.argv[1])).get('pid') or '')" "$HB" 2>/dev/null || true)"
  hb_sup="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('supervisor_pid') or '')" "$HB" 2>/dev/null || true)"
  [[ -n "${hb_sup:-}" ]] && sup_pid="${sup_pid:-$hb_sup}"
  [[ -n "${hb_child:-}" ]] && run_pid="${run_pid:-$hb_child}"
fi
if [[ -f "$STATUS" ]]; then
  st_pid="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('pid') or '')" "$STATUS" 2>/dev/null || true)"
  [[ -n "${st_pid:-}" ]] && run_pid="${run_pid:-$st_pid}"
fi

sup_al=no
run_al=no
alive "$sup_pid" && sup_al=yes
alive "$run_pid" && run_al=yes
echo "supervisor/runner alive: supervisor=${sup_al} pid=${sup_pid:-none} runner=${run_al} pid=${run_pid:-none}"

restarts=0
if [[ -f "$ALERTS" ]]; then
  restarts="$(python3 -c "import json,sys; n=0
for line in open(sys.argv[1],encoding='utf-8'):
    line=line.strip()
    if not line: continue
    ev=json.loads(line)
    if ev.get('event')=='restart': n+=1
print(n)" "$ALERTS")"
fi
echo "restarts count: ${restarts}"

age="missing"
if [[ -f "$HB" ]]; then
  age="$(python3 -c "
import json,sys
from datetime import datetime, timezone
p=sys.argv[1]
d=json.load(open(p))
ts=d.get('ts') or ''
t=datetime.fromisoformat(ts.replace('Z','+00:00'))
if t.tzinfo is None: t=t.replace(tzinfo=timezone.utc)
age=(datetime.now(timezone.utc)-t.astimezone(timezone.utc)).total_seconds()
print(f'{age:.1f}s')
" "$HB")"
fi
echo "last heartbeat age: ${age}"

if [[ -f "$ALERTS" ]]; then
  python3 -c "
import json,sys
from collections import Counter
c=Counter(); err=Counter()
path=sys.argv[1]
for line in open(path,encoding='utf-8'):
    line=line.strip()
    if not line: continue
    ev=json.loads(line)
    typ=ev.get('event') or 'log'
    c[typ]+=1
    if str(ev.get('level','')).lower() in {'error','critical'}:
        err[typ]+=1
parts=[f\"{k}={v}\" for k,v in sorted(c.items())] or ['none=0']
err_parts=[f\"{k}={v}\" for k,v in sorted(err.items())] or ['none=0']
print('error/alert counts by type: alerts ' + ','.join(parts) + '; errors ' + ','.join(err_parts))
" "$ALERTS"
else
  echo "error/alert counts by type: alerts none=0; errors none=0"
fi

iters="missing"
errn="missing"
if [[ -f "$STATUS" ]]; then
  iters="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('iterations','missing'))" "$STATUS")"
  errn="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('errors','?'));" "$STATUS")"
fi
echo "iterations: ${iters} (runner_errors=${errn})"
