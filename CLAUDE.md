# kabu-screener

自分用の株スクリーニングツール。設計方針・データソース・使い方は [README.md](README.md) が正本 (このファイルは作業状態のメモ)。

## 現在地 (2026-08-22 時点)

**EDINET API 移行を実装済み。初回取得はまだ (APIキー待ち)。**

- IR BANK 経由の初回バックフィルはレート制限で 53/3,734社 で停止したまま → 方針転換し、財務+大株主は EDINET API (金融庁公式) から取る `fetch_edinet` を実装した
- 実装は合成データのユニットテストのみで検証済み。**実際の EDINET API・実データではまだ動かしていない**

## 次のタスク

1. https://api.edinet-fsa.go.jp で API キーを無料登録し `EDINET_API_KEY` に設定 (どこにも記録が無いことは確認済み。未登録のはず)
2. まず小さく実データ検証: `fetch_edinet --limit 20` → sqlite で financials / holders の中身と `fetch_log` の no_data / error を目視
   - XBRL の要素IDが会計基準・業種で分かれるため、候補リスト (`parse_edinet.py` の `SUMMARY_ELEMENTS`) に漏れがあれば足す。生ZIPは `data/raw_edinet/` に残るので再ダウンロード不要で試行錯誤できる
3. 問題なければ全社取り込み (`fetch_edinet`、0.5秒間隔で2時間弱) → `screen --stats` で分布確認 → README の日次運用 (cron) へ
4. 残ロードマップ (J-Quants で時価総額、大量保有ウォッチ、役員名簿突き合わせ) は README 末尾参照

## 守ること

- **EDINET は公式APIだが節度を守る**。既定0.5秒間隔より詰めない
- **IR BANK は個人運営サイト** (補完用に残置)。`fetch_irbank.py` のアクセスマナー (スリープ + 連絡先入りUA) は変更しない。制限回避のための偽装 (UA偽装・並列化・プロキシ) はしない

## コマンド

```bash
.venv/bin/python -m unittest discover tests -v   # テスト
.venv/bin/python -m src.fetch_edinet --help      # EDINET取得オプション確認
sqlite3 data/screener.db                         # DB確認
```
