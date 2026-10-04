#!/bin/zsh
# Serial B5 matrix driver: run one actor, then its independent readback (if it prints one); stop on first failure.
# Usage: backend/tests/live/b5_matrix_run_serial.sh "<actor.py> <arg>" ...
set -u
cd "$(dirname "$0")/../../.."
for spec in "$@"; do
  actor=${spec%% *}; arg=${spec#* }; tag="${actor%.py}-${arg}"
  log="${B5_LIVE_EVIDENCE_DIR:?}/serial-${tag}.log"
  "${B5_LIVE_PYTHON:?}" "backend/tests/live/${actor}" ${=arg} > "$log" 2> "${log%.log}-stderr.log"
  rc=$?
  last=$(grep '^{' "$log" | tail -1)  # JSON line from stdout only; stderr is kept separately
  echo "ACTOR ${tag} rc=${rc} ${last[1,240]}"
  [[ $rc -ne 0 ]] && exit 1
  [[ -z "$last" ]] && { echo "ACTOR ${tag} exited 0 but printed no JSON line on stdout"; exit 1; }
  readback=$(print -r -- "$last" | "$B5_LIVE_PYTHON" -c 'import sys,json
try: print(json.loads(sys.stdin.read()).get("readback") or "")
except Exception: print("")')
  # A PASS proof that carries a baseline must have produced a readback command.
  if [[ -z "$readback" ]]; then
    missing=$(print -r -- "$last" | "$B5_LIVE_PYTHON" -c 'import sys,json
try:
    d = json.loads(sys.stdin.read())
    if d.get("status") == "PASS" and d.get("proof"):
        ev = json.load(open(d["proof"])).get("evidence") or {}
        print("1" if ev.get("baseline") else "")
except Exception:
    print("")')
    if [[ -n "$missing" ]]; then echo "READBACK ${tag} MISSING for PASS proof with baseline"; exit 1; fi
  fi
  if [[ -n "$readback" ]]; then
    ${=readback} > "${log%.log}-readback.log" 2>&1
    rr=$?
    echo "READBACK ${tag} rc=${rr} $(tail -1 "${log%.log}-readback.log" | cut -c1-200)"
    [[ $rr -ne 0 ]] && exit 1
  fi
  finalize=""
  if [[ "$actor" == "b5_matrix_checkpoint_faults.py" && ( "$arg" == "db-commit" || "$arg" == "upload" ) ]]; then
    rid=$("$B5_LIVE_PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["evidence"]["run_id"])' "$B5_LIVE_EVIDENCE_DIR/b5-matrix-checkpoint-${arg}-fault.json")
    [[ -z "$rid" ]] && { echo "FINALIZE ${tag} no evidence.run_id in proof"; exit 1; }
    finalize="$B5_LIVE_PYTHON backend/tests/live/b5_matrix_checkpoint_faults.py finalize ${rid}"
  fi
  if [[ -n "$finalize" ]]; then
    ${=finalize} > "${log%.log}-finalize.log" 2>&1
    frc=$?
    echo "FINALIZE ${tag} rc=${frc} $(tail -1 "${log%.log}-finalize.log" | cut -c1-160)"
    [[ $frc -ne 0 ]] && exit 1
  fi
done
echo "SERIAL DONE"
