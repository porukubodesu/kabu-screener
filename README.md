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
| 財務(業績/財務/CF/配当) | EDINET API(有報の主要な経営指標) | 5期分。営業利益のみPL本体から2期分補完 |
| 大株主(当期末の上位10名) | EDINET API(有報の大株主の状況) | 過去履歴はIR BANK由来分があれば併存 |
| (補完) 財務・大株主履歴 | IR BANK(公式配布CSV / holderページ) | EDINETで取れないものだけ任意で |

- **EDINET API** は金融庁の公式API。無料のAPIキーを https://api.edinet-fsa.go.jp で登録し、`EDINET_API_KEY` 環境変数で渡す。公式とはいえ0.5秒間隔の礼儀は守る(初回全社でも2時間弱)
- **IR BANK** は個人運営サイトなので、CSV12秒/HTML3秒間隔+連絡先入りUAでアクセスする(fetch_irbank.pyに実装済み。変更しないこと)

## 使い方

```bash
export EDINET_API_KEY=...                         # 0. EDINETのAPIキー(無料登録)
.venv/bin/python -m src.fetch_jpx                 # 1. 銘柄マスタ更新
.venv/bin/python -m src.fetch_edinet              # 2. 有報から財務+大株主を取得(初回〜2時間)
.venv/bin/python -m src.screen --stats            # 3. 指標の分布を見る(基準の感触を掴む)
.venv/bin/python -m src.screen                    # 4. スクリーニング実行(結果はDBにも保存)
.venv/bin/python -m src.notify                    # 5. 今日の1銘柄を通知(LINE or 標準出力)
```

テスト: `.venv/bin/python -m unittest discover tests -v`

### 日次運用(cron)

初回取得後の日次実行は差分だけで軽い(書類一覧の未走査日+新しく提出された有報のみ):

```bash
# 毎朝: 新着の有報を取り込み → スクリーニング → 1銘柄通知
.venv/bin/python -m src.fetch_jpx
.venv/bin/python -m src.fetch_edinet
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

- **営業利益は2期分のみ**(有報の主要指標に含まれず、PL本体から当期・前期を補完)。増益ストリークは営業利益が足りない場合、経常利益→純利益の順で代用する(screen.py)
- **要素IDのカバレッジは実データで要検証**。会計基準・業種でXBRLタグが分かれるため候補リスト(parse_edinet.py)で吸収しているが、初回取り込み後に `fetch_log` の no_data / error を確認して候補を足す
- **時価総額・株価なし** → J-Quants(要無料アカウント)を足す。無料プランは12週遅延だが年次スクリーニングには影響なし
- **オーナー判定は名前ベースのヒューリスティック**(法人マーカーに当たらなければ個人とみなす)。有報の役員の状況との突き合わせは未実装
- **大量保有報告書の著名人ウォッチ**(イベント駆動通知)は未実装 → 同じEDINET APIの書類一覧(docTypeCode=350)で実装予定
