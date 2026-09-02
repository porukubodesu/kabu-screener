# kabu-screener

自分用の株スクリーニングツール。「ちゃんと増収増益していてキャッシュフローが良く、できればオーナー(創業者・個人)が大株主」の銘柄を見つける。

## 設計方針

- **判定機ではなく発見機**。ハードフィルタは最小限(営業CF赤字なし+事業内容NGワードのみ。オーナー保有率はスコアで加点、VCは除外せず一覧・通知に表示して人間が判断)にして、他の指標はパーセンタイル(全上場企業内の相対位置)でスコア化し、上位から人間が見る。閾値の発明をしない。
- **DBファースト**。全上場企業のデータをSQLiteに持ち、スクリーニング条件はSQLとPython定数の書き換えだけで何度でも試行錯誤できる。通知はDBの上の薄い層。
- **四季報は定性チェック用**。独自予想と記者コメントは機械可読で取れないので、候補銘柄が通知されたら証券口座(SBI/楽天なら無料)で読む運用。

## データソース

| データ | ソース | 備考 |
|---|---|---|
| 上場銘柄マスタ | JPX公表 `data_j.xls` | プライム/スタンダード/グロース内国株式のみ(約3,700社) |
| 財務(業績/財務/CF/配当) | **EDINET API v2**(有報のCSV) | 「主要な経営指標等の推移」で5年分。金融庁の公式API |
| 大株主 | EDINET(有報の年次)+ IR BANK `/{code}/holder`(半期履歴) | 時点単位で共存 |
| 事業内容 | EDINET(有報「事業の内容」) | 全文を`business`テーブルに保存。NGワード除外+一覧・通知に表示 |
| 財務(移行前のフォールバック・業績予想) | IR BANK `fy-data-all.csv`(公式配布) | 直近4〜5年分。EDINET取り込み済み銘柄はスキップ |
| 終値・時価総額 | **J-Quants API v2**(JPX公式、無料プラン) | 12週遅延。https://jpx-jquants.com/ で無料登録 → ダッシュボードでAPIキー発行 → `.env` に `JQUANTS_API_KEY`。未設定なら時価総額欄は「-」 |

EDINET APIは無料だがAPIキーが必要。EDINETトップページ https://disclosure2.edinet-fsa.go.jp/ の「EDINET API」からユーザー登録(メール+電話番号)→マイページでキー発行→ `export EDINET_API_KEY=...`。

IR BANKは個人運営サイトなので、リクエスト間スリープ+連絡先入りUAでアクセスする(fetch_irbank.pyに実装済み。変更しないこと)。特にCSV配布サーバー(f.irbank.net)は**約10件/30分**で表示制限になるため、CSV取得間隔は既定190秒にしている。

## 使い方

```bash
.venv/bin/python -m src.fetch_jpx                 # 1. 銘柄マスタ更新
.venv/bin/python -m src.fetch_edinet --days 400   # 2. 有報5年分を取得(初回は約2時間)
.venv/bin/python -m src.screen --stats            # 3. 指標の分布を見る(基準の感触を掴む)
.venv/bin/python -m src.screen                    # 4. スクリーニング実行(結果はDBにも保存)
.venv/bin/python -m src.fetch_prices              # 5. 終値取得(J-Quants設定時のみ。時価総額用)
.venv/bin/python -m src.notify                    # 6. 今日の1銘柄を通知(LINE or 標準出力)
.venv/bin/python -m src.report                    # 7. 結果ページ生成(data/site/index.html)
.venv/bin/python -m src.discover                  # 8. 発見機② テーマ×事業モデル(discover.html)
.venv/bin/python -m src.rates                     # 9. 発見機③ 金利上昇(rates.html)
.venv/bin/python -m src.rates --check             #    参照銘柄(地銀・生保・JPX・任天堂等)が何位かだけ見る
```

### 発見機③ 金利上昇 (`src/rates.py`)

「金利が上がった時に強い銘柄」を、推奨ではなく**研究対象**として5分類ごとに並べる。
分類内パーセンタイルで採点し、分類をまたぐ比較はしない。

| 分類 | 母集団 | 採点 |
|---|---|---|
| ① 銀行 | 33業種=銀行業 | 経常利益 2024年度→直近の実測増益率 50% / 連続経常増益 25% / ROE 25% |
| ② 保険 | 保険業 | 同上(運用利回り改善は銀行に遅行して効く) |
| ③ 証券・取引所 | 証券、商品先物取引業 + 社名に「取引所」 | 同上 |
| ④ ネットキャッシュ | 金融以外で 現預金−総負債>0 | 絶対額 / 対時価総額 / 金利+1%の利益押上げ(ネットキャッシュ×1%÷純利益) を等分 |
| ⑤ 不動産(選別) | 不動産業 | 自己資本比率 50% / 実測増益率 50%(多くは借入負担で弱い側。上位だけ見る) |

貸出比率・再編の主導/被統合・有利子負債はDBに無い。代わりに「利上げ局面(2024/03の
マイナス金利解除以降)で実際に経常利益が伸びたか」を実測の金利感応度として使い、
規模区分(Core30/Large70=大手)とPBRをカードに出して人間が補う。Jリートは銘柄マスタ外。
`REFERENCE_CODES`(千葉銀・しずおかFG・第一生命・JPX・任天堂など)は発見機②の教師データと
同じ「載るべき社が載っているか」の逆算チェック用で、生成時と `--check` で位置を表示する。

IR BANK版の取得(`src.fetch_irbank`)は業績予想と大株主の半期履歴の補完用。EDINET取り込み済みの銘柄では財務CSVを自動でスキップする。

テスト: `.venv/bin/python -m unittest discover tests -v`

### 日次運用(launchd)

毎朝の「新着有報の差分取り込み → 大株主の鮮度維持 → スクリーニング → 1銘柄通知」は
[scripts/daily.sh](scripts/daily.sh) にまとまっている(EDINETは差分取得なので数分で終わる)。
macOSはスリープ中cronを取りこぼすので、launchdで毎朝7時に回す
(7時に寝ていても次の起床時に実行される):

```bash
# 登録(~/Library/LaunchAgents/com.porukubodesu.kabu-screener.plist が daily.sh を毎朝7時に実行)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.porukubodesu.kabu-screener.plist
launchctl bootout gui/$(id -u)/com.porukubodesu.kabu-screener   # 解除
tail -f data/daily.log                                          # 実行ログ
```

APIキーとLINEの認証情報はプロジェクトルートの `.env`(gitignore済み)に置く。
daily.sh が実行時に読み込む。

### 結果ページ(GitHub Pages)

日次実行の最後に `src.report` / `src.discover` / `src.rates` が静的HTML(発見機①②③)を生成し、
**単一コミットの `site` ブランチに強制push**して公開する(masterは汚さない):

**https://porukubodesu.github.io/kabu-screener/**

LINE通知は任意(未設定ならログ出力のみ)。ページがあれば実質不要。

### LINE通知

環境変数を設定するとLINE Messaging APIでpushする(未設定なら標準出力のみ):

```bash
export LINE_CHANNEL_ACCESS_TOKEN=...   # 秘書ボットのチャネルアクセストークン
export LINE_USER_ID=...                # 自分のユーザーID
```

## 条件の試行錯誤

`src/screen.py` 冒頭の定数を書き換える:

- `MIN_OWNER_RATIO` / `EXCLUDE_VC` / `REQUIRE_OP_CF_NONNEG` — ハードフィルタ
  (既定はオーナー0%=無効・VC除外オフ・営業CF赤字なしのみ有効。締めたくなったらここを戻す)
- `NG_BUSINESS_KEYWORDS` — 「明らかに微妙な事業」の除外ワード(有報「事業の内容」全文への部分一致。
  事業内容はDBに全文吸い上げてあるので、ここを書き換えるだけで除外条件を試行錯誤できる)
- `WEIGHTS` — スコアの重み(売上CAGR、連続増収増益、オーナー比率、自己資本比率。
  CFマージン・非希薄化は2026-08-28にスコアから除外し表示のみ)
- 「ちゃんと増収」のラインは `--stats` で全社の分布を見て、好きな企業(逆算キャリブレーション)がどこに居るか確認して決める

## 既知の制約とロードマップ

- **営業利益の履歴が浅い**: 有報サマリーに営業利益が無い会社(日本基準)は財務諸表本体から当期+前期の2年分しか取れない。毎年の取り込みで蓄積されるほか、IR BANK由来の履歴があれば年度マージで保全される
- **業績予想が取れない**: 予想は決算短信(TDnet)由来でEDINETに無い。スクリーニングは実績のみ使うので影響なし(予想が欲しくなったらIR BANK併用のまま)
- **株式分割**: 希薄化は(株主資本 or 純資産)/BPSで近似。有報サマリーは分割を「実施期の翌期首起点」で仮定計算するため最古年度のBPSが未修正のことがあり(例: 9983の2021/08)、IR BANK由来の古い年度との境界でも誤差が出うる → 分割イベントはJ-Quants導入時に
- **時価総額・終値は12週遅延**: J-Quants無料プランの制約。時価総額はAPIの公式値(MktCap)を使用、無い銘柄のみ終値×推定株式数(純資産÷BPS)で概算。遅延なし化は有料プラン検討
- **オーナー判定は名前ベースのヒューリスティック**(法人マーカーに当たらなければ個人とみなす)。有報の役員名簿との突き合わせは未実装
- **大量保有報告書の著名人ウォッチ**(イベント駆動通知)は未実装 → 書類一覧APIの docTypeCode=350 で実装できる素地はできた
