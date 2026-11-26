#!/usr/bin/env bash
# 小批更正手工冒烟：装载 1×1 小车型 -> 补录一个配对 -> 幂等重复标有
# -> 补一个备件 -> 反例拒收。用法：
#   BASE_URL=http://localhost:8000 bash scripts/smoke_small_batch.sh
set -u
BASE_URL="${BASE_URL:-http://localhost:8000}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/examples"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }

say "health"
curl -sS "$BASE_URL/" ; echo

say "load 1x1 DEMO-TINY"
curl -sS -X POST "$BASE_URL/api/v1/models/load" \
  -H 'Content-Type: application/json' \
  --data @"$DIR/load_tiny.json" ; echo

say "correct: add ONE pair (P-1,C-B) -> 1x2 rectangle"
curl -sS -X POST "$BASE_URL/api/v1/models/correct" \
  -H 'Content-Type: application/json' \
  --data @"$DIR/correct_add_one_pair.json" ; echo

say "correct: repeat plus existing (P-1,C-A) -> retained, no row changes"
curl -sS -X POST "$BASE_URL/api/v1/models/correct" \
  -H 'Content-Type: application/json' \
  --data @"$DIR/correct_repeat_existing_pair.json" ; echo

say "correct: add ONE spare part P-2 (two pairs, one new part row)"
curl -sS -X POST "$BASE_URL/api/v1/models/correct" \
  -H 'Content-Type: application/json' \
  --data @"$DIR/correct_add_one_part.json" ; echo

say "correct: remove one corner pair -> 422 with witness, tables untouched"
curl -sS -i -X POST "$BASE_URL/api/v1/models/correct" \
  -H 'Content-Type: application/json' \
  --data @"$DIR/correct_reject_missing_corner.json" | sed -n '1p;/detail/,$p'

say "final state"
curl -sS "$BASE_URL/api/v1/models/DEMO-TINY" ; echo
