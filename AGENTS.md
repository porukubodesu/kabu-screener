# AGENTS.md

プロジェクト概要・設計はREADME.md、作業状態はCLAUDE.mdを参照。

## レビュー・作業時の注意

- `parse_edinet.py` の FIELD_SPECS / ELEMENT_SPECS は**実データ検証済み**
  (7203/9983/130AのIR BANK値との突き合わせ+全量3,753書類でエラーゼロ)。
  要素IDの実在性をWeb検索等で裏取りする必要はない
- `fetch_irbank.py` のアクセスマナー (スリープ・連絡先入りUA・190秒のCSV間隔) は
  個人運営サイトへの配慮として意図的な仕様。高速化・並列化の提案は不要
- `data/` 配下 (DB・生zip・生成物 `data/site/index.html`) はgitignore対象の生成物
- `src/report.py` の出力は**公開ページ** (GitHub Pages)。HTMLエスケープ漏れは重大
- テストは `.venv/bin/python -m unittest discover tests`
