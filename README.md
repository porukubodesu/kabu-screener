# kabu-screener

自分用の株スクリーニングツール。「オーナー(創業者・個人)が大株主で、VCがおらず、ちゃんと増収増益していてキャッシュフローが良い」銘柄を見つける。

## 設計方針

- **判定機ではなく発見機**。ハードフィルタは最小限(オーナー保有率・VC不在・営業CF赤字なし)にして、他の指標はパーセンタイル(全上場企業内の相対位置)でスコア化し、上位から人間が見る。閾値の発明をしない。
- **DBファースト**。全上場企業のデータをSQLiteに持ち、スクリーニング条件はSQLとPython定数の書き換えだけで何度でも試行錯誤できる。通知はDBの上の薄い層。
- **四季報は定性チェック用**。独自予想と記者コメントは機械可読で取れないので、候補銘柄が通知されたら証券口座(SBI/楽天なら無料)で読む運用。

## データソース

| データ | ソース | 備考 |
|---|---|---|
| 上場銘柄マスタ | JPX公表 `data_j.xls` | プライム/スタンダード/グロース内国株式のみ(約3,700社) |
| 財務(業績/財務/CF/配当) | IR BANK `fy-data-all.csv`(公式配布) | 直近4〜5年分 |
| 大株主(半期スナップショット履歴) | IR BANK `/{code}/holder` | 有報・大量保有由来 |

IR BANKは個人運営サイトなので、リクエスト間1.5秒スリープ+連絡先入りUAでアクセスする(fetch_irbank.pyに実装済み。変更しないこと)。

## 使い方

```bash
.venv/bin/python -m src.fetch_jpx                 # 1. 銘柄マスタ更新
.venv/bin/python -m src.fetch_irbank              # 2. 財務+大株主を取得(初回は数時間)
.venv/bin/python -m src.screen --stats            # 3. 指標の分布を見る(基準の感触を掴む)
.venv/bin/python -m src.screen                    # 4. スクリーニング実行(結果はDBにも保存)
.venv/bin/python -m src.notify                    # 5. 今日の1銘柄を通知(LINE or 標準出力)
```

テスト: `.venv/bin/python -m unittest discover tests -v`

### 日次運用(cron)

初回取得後は、鮮度維持のローリング更新で十分:

```bash
# 毎朝: 30日より古い銘柄を200社ずつ再取得 → スクリーニング → 1銘柄通知
.venv/bin/python -m src.fetch_jpx
.venv/bin/python -m src.fetch_irbank --stale-days 30 --limit 200
.venv/bin/python -m src.screen
.venv/bin/python -m src.notify
```

### LINE通知

環境変数を設定するとLINE Messaging APIでpushする(未設定なら標準出力のみ):

```bash
export LINE_CHANNEL_ACCESS_TOKEN=...   # 秘書ボットのチャネルアクセストークン
export LINE_USER_ID=...                # 自分のユーザーID
```

## 条件の試行錯誤

`src/screen.py` 冒頭の定数を書き換える:

- `MIN_OWNER_RATIO` / `EXCLUDE_VC` / `REQUIRE_OP_CF_NONNEG` — ハードフィルタ
- `WEIGHTS` — スコアの重み(売上CAGR、連続増収増益、CFマージン、オーナー比率、自己資本比率、非希薄化)
- 「ちゃんと増収」のラインは `--stats` で全社の分布を見て、好きな企業(逆算キャリブレーション)がどこに居るか確認して決める

## 既知の制約とロードマップ

- **財務が直近4〜5年分**(IR BANK無料CSVの範囲)。「5期連続」の厳密判定には1年足りない → EDINET API(要無料APIキー登録)に移行すれば有報の5年サマリーで解消
- **時価総額・株価なし** → J-Quants(要無料アカウント)を足す。無料プランは12週遅延だが年次スクリーニングには影響なし
- **オーナー判定は名前ベースのヒューリスティック**(法人マーカーに当たらなければ個人とみなす)。役員名簿との突き合わせはEDINET移行時に
- **大量保有報告書の著名人ウォッチ**(イベント駆動通知)は未実装 → EDINET APIで実装予定
