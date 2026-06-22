# Nature系列 OA新着 日次ダイジェスト

`feeds.txt` に並べた Nature 系列誌のRSSを毎日チェックし、**オープンアクセス(OA)の新着論文だけ**を
Gemini で日本語に要約・翻訳して `output/YYYY-MM-DD.md` に書き出します。
GitHub Actions で全自動（手動ボタン実行も可）。

## 仕組み

1. `feeds.txt` の各RSSを取得
2. `state/seen.json` と照合して**新着のみ**抽出（重複DOIは自動除去）
3. **Unpaywall** で DOI ごとに OA 判定 → OA だけ残す
4. **Gemini** で `prompts/` のプロンプトに従い日本語の要約・翻訳を生成
5. Markdown に書き出し、`seen.json` を更新（結果はリポジトリへ自動コミット）

```
feeds.txt                 監視対象RSS（編集するだけで増減できる）
prompts/summarize.txt     要約・解釈プロンプト（後から自由に編集）
prompts/translate.txt     翻訳プロンプト
src/main.py               本体
state/seen.json           既処理DOIの記録（自動更新）
output/                   日付ごとのダイジェスト
.github/workflows/daily.yml  定期実行の設定
```

## セットアップ（5ステップ）

1. **このフォルダをGitHubリポジトリにアップロード**（Public推奨。Actionsが完全無料になります）。
2. **Gemini APIキーを取得**：Google AI Studio で発行。
3. **Secrets を登録**：リポジトリの `Settings > Secrets and variables > Actions > Secrets` で
   - `GEMINI_API_KEY` … Geminiのキー
   - `CONTACT_EMAIL` … 自分のメール（Unpaywall API に必須）
4. **（任意）挙動を変えたいとき**は同画面の `Variables` で設定：
   - `GEMINI_MODEL`（既定 `gemini-2.5-flash`）
   - `MAX_ITEMS_PER_RUN`（既定 `40`／1回の処理上限。無料枠の安全弁）
   - `REQUEST_DELAY_SEC`（既定 `5`／API呼び出し間隔）
   - `OA_ONLY`（既定 `true`／`false`にすると非OAも要旨だけ収録）
5. **初回だけ「シード実行」**：`Actions` タブ → `daily-nature-oa-digest` → `Run workflow` →
   **seed_only を true** で実行。これで現時点の全記事を「既読」にするだけ（要約しない）ので、
   初回に大量処理して無料枠を使い切る事故を防げます。**翌日以降は本当の新着だけ**が処理されます。

以降は毎日自動実行されます（既定はUTC 22:00＝日本時間07:00）。

## カスタマイズ

- **対象誌の増減**：`feeds.txt` を編集するだけ。
- **プロンプト変更**：`prompts/*.txt` を編集（コード変更不要）。ファクトチェック等を足す場合は
  プロンプトファイルを追加し、`src/main.py` の `build_section` に数行追加すれば拡張できます。
- **実行時刻**：`.github/workflows/daily.yml` の `cron`（UTC表記）を編集。

## 注意点（無料運用の前提）

- **OAのみが対象**です。Natureなど購読誌の有料論文は要旨も含め処理しません（OA判定で除外）。
  npj系・Communications系・Nature Communications は基本的に全記事OAなので多くが対象になります。
- **Gemini無料枠は1日単位**（Flash系でおおむね1日1,000〜1,500リクエスト、太平洋時間0時リセット）。
  本用途では十分ですが、上限や仕様は変わるため Google AI Studio で要確認。無料枠は入力が
  モデル改善に使われる点にも留意（機微な情報は流さない）。
- **GitHub Actionsの定期実行**は混雑時に数分〜十数分遅れることがあります（時刻厳守用途でなければ問題なし）。
- **60日間リポジトリに活動がないとスケジュールが自動停止**します。本ワークフローは毎日結果をコミットするため、
  通常は自動的に活動が続きます。
- LLMの「ファクトチェック」は、要約が要旨から逸脱していないかの整合性確認には有効ですが、
  論文の科学的真偽の検証はできません。各記事のDOIリンクから必ず原文を確認してください。
