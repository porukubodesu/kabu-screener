#!/bin/bash
# 日次運用 (README「日次運用」参照)。launchdから毎朝呼ばれる。
# 手動実行も可: bash scripts/daily.sh
# 1ステップ失敗しても後続は実行する(取得が転けてもスクリーニング・通知は手元のDBで動く)
cd "$(dirname "$0")/.." || exit 1
set -a; source .env 2>/dev/null; set +a
PY=.venv/bin/python
LOG=data/daily.log

{
  echo "===== daily run $(date '+%Y-%m-%d %H:%M:%S') ====="
  $PY -m src.fetch_jpx
  $PY -m src.fetch_edinet --days 7
  $PY -m src.fetch_irbank --stale-days 30 --limit 200
  $PY -m src.screen
  $PY -m src.notify
  echo "===== done $(date '+%Y-%m-%d %H:%M:%S') ====="
} >> "$LOG" 2>&1
