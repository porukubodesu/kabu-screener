# kabu-screener

自分用の株スクリーニングツール。設計方針・データソース・使い方は [README.md](README.md) が正本 (このファイルは作業状態のメモ)。

## 現在地 (2026-08-15 時点)

**IR BANKバックフィルは完走 (3681/3681、ok=3669 / no_data=12 / error=0、プロセス停止済み)。**
財務3,722銘柄+大株主3,716銘柄がDBに入り、IR BANKデータだけでスクリーニングは動く状態。

**EDINET移行コードは実装済みだが、実APIキー未登録のため実データ未投入**
(financialsは `source='irbank'` のみ)。

- `src/fetch_edinet.py` + `src/parse_edinet.py`: 有報 (docTypeCode=120) のCSVから
  経営指標サマリー5年分+営業利益 (財務諸表本体、当期+前期) +大株主+事業の内容を取り込む
- IR BANK由来の行とは年度単位でマージ (EDINET優先、単位はどちらも生の円で一致確認済み)
- 事業内容は「事業の内容」TextBlockの全文を `business` テーブルに保存し (吸い上げてから絞る方式)、
  `screen.py` の `NG_BUSINESS_KEYWORDS` でスクリーニング時に除外+一覧・通知カードに表示。
  EDINET取り込みまでは空なので除外は発動しない
- **要素IDラベルのマッピング (`parse_edinet.FIELD_SPECS`) は仕様書+記事ベースで、実データ未照合。**
  比率が純小数 (0.585) との仮定も未検証 (`RATIO_FIELDS` のTODO参照)

## 次のタスク

1. **[ユーザー作業] EDINET APIキー登録**: https://disclosure2.edinet-fsa.go.jp/ の
   「EDINET API」からユーザー登録 (メール+電話番号、無料) → マイページでキー発行 →
   `export EDINET_API_KEY=...`
2. 実データで検証: `.venv/bin/python -m src.fetch_edinet --days 400 --codes 7203,9983,130A`
   → `sqlite3 data/screener.db "SELECT * FROM financials WHERE code='7203'"` をIR BANK値と
   突き合わせ、FIELD_SPECSのマッピング崩れ (特に比率の単位、銀行・IFRS銘柄のラベル) を直す。
   `business` テーブルの事業内容テキストも目視確認 (HTML除去の崩れ)
3. 全量取り込み: `--days 400` (書類一覧400日分の走査 約7分 + 約3,700書類 約2時間)
4. cron移行 (README「日次運用」参照)。以降IR BANKは大株主の鮮度維持+業績予想のみ

## 守ること

- **IR BANK は個人運営サイト**。`fetch_irbank.py` のアクセスマナー (スリープ + 連絡先入りUA) は変更しない
- 制限回避のための偽装 (UA偽装・並列化・プロキシ) はしない。ペースを落とす方向でのみ調整する
- f.irbank.net (CSV) の実測クォータは**約10件/30分**。`--csv-sleep` は190秒未満にしない
  (既定値も190秒にしてある)

## コマンド

```bash
.venv/bin/python -m unittest discover tests -v   # テスト
.venv/bin/python -m src.fetch_edinet --help      # EDINET取得オプション
.venv/bin/python -m src.fetch_irbank --help      # IR BANK取得オプション
sqlite3 data/screener.db                         # DB確認
```

注意: プロジェクト移動の名残で `.venv/bin/pip` はshebangが壊れている。
`.venv/bin/python -m pip` を使うこと。
