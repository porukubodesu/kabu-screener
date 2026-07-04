# kabu-screener

自分用の株スクリーニングツール。設計方針・データソース・使い方は [README.md](README.md) が正本 (このファイルは作業状態のメモ)。

## 現在地 (2026-07-04 時点)

**初回バックフィルの途中で停止している。**

- 銘柄マスタ: 3,734社 取得済み (`companies`)
- 財務データ: **53社のみ** (`financials`) — 残り約3,680社が未取得
- 停止理由: IR BANK の表示制限 (`csv blocked`) に繰り返し当たり、30分待機ループのまま終了 (`data/backfill.log` 参照)

## 次のタスク

1. IR BANK のレート制限に当たらないペースで初回取得を完走させる
   - 現状の 1.5秒スリープでも制限に当たった実績あり → 間隔を伸ばす / `--limit` で1日分を絞って数日かけて埋める、のどちらかから検討
   - `--stale-days` / `--limit` オプションは `fetch_irbank` に実装済み
2. 完走後は README の「日次運用 (cron)」セクションの通りローリング更新に移行
3. ロードマップ (EDINET API 移行、J-Quants で時価総額、大量保有ウォッチ) は README 末尾参照

## 守ること

- **IR BANK は個人運営サイト**。`fetch_irbank.py` のアクセスマナー (スリープ + 連絡先入りUA) は変更しない (README にも明記)
- 制限回避のための偽装 (UA偽装・並列化・プロキシ) はしない。ペースを落とす方向でのみ調整する

## コマンド

```bash
.venv/bin/python -m unittest discover tests -v   # テスト
.venv/bin/python -m src.fetch_irbank --help      # 取得オプション確認
sqlite3 data/screener.db                         # DB確認
```
