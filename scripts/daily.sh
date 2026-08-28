#!/bin/bash
# 日次運用 (README「日次運用」参照)。launchdから毎朝呼ばれる。
# 手動実行も可: bash scripts/daily.sh
# 1ステップ失敗しても後続は実行する(取得が転けてもスクリーニング・通知は手元のDBで動く)
cd "$(dirname "$0")/.." || exit 1
set -a; source .env 2>/dev/null; set +a
# launchdのPATHは最小構成でgh等が見えないため明示的に足す
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
PY=.venv/bin/python
LOG=data/daily.log
mkdir -p data  # 初回チェックアウト時はdata/が無く、ログのリダイレクトに先に失敗する

{
  echo "===== daily run $(date '+%Y-%m-%d %H:%M:%S') ====="
  $PY -m src.fetch_jpx
  $PY -m src.fetch_edinet --days 7
  $PY -m src.fetch_irbank --stale-days 30 --limit 200
  $PY -m src.screen
  $PY -m src.fetch_prices
  $PY -m src.notify
  # サイト公開: 2ページ(発見機①②)を生成し、単一コミットの site ブランチとして
  # 強制push (masterの履歴を日次コミットで汚さないためのplumbing方式)。
  # 生成が失敗したら古いページを再公開しないようpushしない
  if $PY -m src.report && $PY -m src.discover; then
    B1=$(git hash-object -w data/site/index.html)
    B2=$(git hash-object -w data/site/discover.html)
    TREE=$(printf '100644 blob %s\tindex.html\n100644 blob %s\tdiscover.html\n' \
                  "$B1" "$B2" | git mktree)
    COMMIT=$(git commit-tree "$TREE" -m "site: $(date '+%Y-%m-%d')")
    git branch -f site "$COMMIT"
    # launchd環境ではosxkeychainが効かないことがあるためghを直指定
    git -c credential.helper='!/usr/local/bin/gh auth git-credential' \
        push -f origin site
  fi
  echo "===== done $(date '+%Y-%m-%d %H:%M:%S') ====="
} >> "$LOG" 2>&1
