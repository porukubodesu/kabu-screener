# kabu-screener

自分用の株スクリーニングツール。設計方針・データソース・使い方は [README.md](README.md) が正本 (このファイルは作業状態のメモ)。

## 現在地 (2026-08-22 時点)

**EDINET全量取り込み完了 (3,753書類 / ok=3753 / error=0)。**
financialsは3,692社が `source='edinet'`、事業内容 (business) は3,691社。
スクリーニング〜通知までEDINETデータで動く状態 (NGワード除外も発動確認済み)。

- APIキーは登録済みで**プロジェクトルートの `.env`** (gitignore済み) にある。
  実行時は `set -a && source .env && set +a` で読む
- FIELD_SPECSは7203/9983/130Aの実データでIR BANK値と突き合わせ検証済み。
  比率は純小数→100倍で正しいことを確認 (RATIO_FIELDSのTODO解決)
- トヨタ型の**企業固有拡張要素** (`...KeyFinancialData`、ラベル列が空) は
  `ELEMENT_SPECS` の要素ID部分一致で対応済み (revenueなど)
- 生の書類zipは `data/raw_edinet/<code>/` に保存済み。パーサ修正時は
  再ダウンロード不要で `save_document()` に食わせ直せば再パースできる

既知の許容事項:
- 生保 (7181/8750) は有報サマリーに経常収益が無い (保険料等収入などの拡張要素のみ)。
  規模の合わない流用はせず、端の年度の売上欠損は許容
- 有報サマリー最古年度 (4期前) のBPS・配当は分割未修正のことがある (9983で実測)。
  希薄化指標が境界で歪む → J-Quants導入時に分割イベントで解決
- ROEはEDINET=有報公式値、IR BANK=独自計算値で微差あり (EDINET優先)
- ハードフィルタは営業CF赤字なしのみ (VC不在・オーナー保有率は非マスト、2026-08-15変更)

## 次のタスク

1. (任意) LINE通知の有効化 (.envにトークン2行)。結果ページがあるので優先度低
2. (任意・ロードマップ) J-Quants導入: 時価総額・株価・分割イベント

## 運用メモ

- 日次はlaunchd (毎朝7時、`scripts/daily.sh`)。plistは
  `~/Library/LaunchAgents/com.porukubodesu.kabu-screener.plist`
- 結果ページ: https://porukubodesu.github.io/kabu-screener/
  (`src.report` が生成 → 単一コミットの `site` ブランチに強制push。
  リポジトリは2026-08-22にpublic化済み、APIキーの履歴非混入は確認済み)

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
