# kabu-screener

自分用の株スクリーニングツール。設計方針・データソース・使い方は [README.md](README.md) が正本 (このファイルは作業状態のメモ)。

## 現在地 (2026-08-28 時点)

**完成・運用中。毎朝7時にlaunchdが全パイプラインを回し、GitHub Pagesに3ページ公開。**

- 発見機① 財務 (index.html): screen.pyのスコア順・業種タブ・上位300カード
- 発見機② テーマ×事業モデル (discover.html): themes.py/discover.py。
  CF赤字許容・受託/コンサル/不動産系除外・テーマ10種タブ・上位400カード。
  教師データ (yutori/GENDA/バイセル/パワーエックス) は全員400位内で検証済み
- 発見機③ 金利上昇 (rates.html): rates.py。銀行/保険/証券・取引所/ネットキャッシュ/
  不動産選別の5分類を分類内パーセンタイルで採点 (2026-09-02追加)。
  貸出比率・有利子負債はDBに無いので「経常利益 2024年度→直近の実測増益率」で代替。
  参照銘柄 (REFERENCE_CODES: 千葉銀・第一生命・JPX・任天堂等) の位置は
  `python -m src.rates --check` で見る。**実データでの検証は未実施** (実装環境にDB無し)
- カードUI: 指標チップ・事業内容・決算推移(表+バー)・月足キャンドル(自前SVG、
  TradingView埋め込みは東証非対応)・大株主。J-Quants v2から終値・時価総額・日足
  (無料プランは12週遅延、日次は追加リクエストゼロで日足が蓄積)

**EDINET全量取り込み完了 (3,753書類 / ok=3753 / error=0)。**
financialsは3,692社が `source='edinet'`、事業内容 (business) は3,691社。

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

0. 発見機③を実データで初回実行して参照銘柄の位置を確認する
   (`.venv/bin/python -m src.rates --check`)。地銀が「圏外」ばかりなら
   経常利益の欠損 (銀行は経常収益/経常利益がサマリーに出るはず) を疑う
1. (任意) themes.pyの辞書をユーザーフィードバックで育てる
   (「この会社が入ってない/余計」ベースの逆算キャリブレーション)
2. (任意) J-Quantsの分割イベントで希薄化指標の境界誤差を解消
3. (任意) LINE通知の有効化 (.envにトークン2行)。結果ページがあるので優先度低

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
.venv/bin/python -m src.rates --check            # 発見機③: 参照銘柄の分類内順位
sqlite3 data/screener.db                         # DB確認
```

注意: プロジェクト移動の名残で `.venv/bin/pip` はshebangが壊れている。
`.venv/bin/python -m pip` を使うこと。
