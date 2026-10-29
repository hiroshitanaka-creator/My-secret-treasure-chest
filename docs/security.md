# セキュリティと課金事故の防止

## API キーの管理

- キーは環境変数 `OPENAI_API_KEY` からだけ読みます。TOML の設定に `api_key` などを書くと、未知のキーとしてエラーになります。
- `.env` は `.gitignore` で除外しています。ひな形は `projects/ai-civilization-lab/.env.example` です。`.env` は `--env-file` を明示したときだけ読み込み、ログには変数の**名前**だけを出します。
- ログと SQLite に保存する文字列（エラー、生の出力、理由）は `redaction.redact` を通します。伏せる対象は、`sk-…` の形をした文字列、`Bearer …`、`*_API_KEY=` や `token:` などの代入形、そして現在の `OPENAI_API_KEY` の値そのものです。
- テストは偽のキーを使い、ネットワーク接続を遮断した状態で動きます。CI には secrets を渡しておらず、`OPENAI_API_KEY` と `CIVLAB_ALLOW_PAID_API` を空に固定しています。

## 有料 API を有効にする条件（すべて必要）

1. 実験の設定ファイルに `[openai] allow_paid_api = true`（真偽値。文字列の `"true"` はエラー）
2. 環境変数 `CIVLAB_ALLOW_PAID_API=yes`
3. 環境変数 `OPENAI_API_KEY`
4. `[budget]` で `max_paid_calls > 0`、`max_input_tokens`、`max_output_tokens`、`max_estimated_cost_usd` を設定
5. `[pricing]` に、公式の料金ページから人が写した単価（0 より大きい値。0 だとコスト上限が効かなくなるため、設定済みとは扱わない）と `source`・`checked_at`

1〜3 のどれかが欠けると、アダプターを作る段階で `PaidApiDisabledError` になります。4〜5 が欠けると、呼び出す前に Budget Guard が拒否します。`civlab check-config` で、今どの条件が欠けているかを確かめられます（通信はしません）。

## 課金を止める条件

- 呼び出し回数、有料呼び出し回数、入力トークン、出力トークン、推定コストのどれかが上限に**達しそうなとき**（呼ぶ前の確認）
- 強制停止：`--stop-file` で指定したファイルがあるとき、または `CIVLAB_KILL_SWITCH=1` のとき。次の呼び出しの前に止まります
- 再試行は 1 回の判断につき最大 `max_retries` 回（設定できる範囲は 0〜5）。SDK 自身の再試行は 0 にしてあります
- 実行が止まった理由は `runs.status` と `errors` に記録されます

**注意（穴として明記します）**：コストは「トークン数 × 設定した単価」による**推定**です。実際の請求額は API の応答に含まれないため取得しておらず、`actual_cost_usd` は常に NULL です。請求額は OpenAI の管理画面で確かめる必要があります。管理画面側で使用量の上限を設定することも推奨します【未確認：現在の設定項目は OpenAI の公式画面で確かめてください】。

## 公開リポジトリの注意事項

- このリポジトリは Public です。コミットした内容（履歴を含む）は誰でも読めます。
- 実験の DB（`runs/`、`*.sqlite3`）は除外しています。LLM の出力やプロンプトに個人情報や秘密を含めないでください。
- `python tools/check_repo_safety.py --history` が、禁止ファイル（`.env`、鍵、DB など）と秘密情報の形をした文字列を、HEAD と全履歴から探します。見つかった値そのものは表示しません。CI でも毎回実行します。
- 秘密情報を誤ってコミットしたときは、履歴から消すより先に、**そのキーを無効化**してください（Public では取得済みと考えるべきです）。

## 権限管理

- GitHub Actions：`permissions: contents: read`、`persist-credentials: false`、Actions はコミット SHA で固定しています。
- Claude Code：`.claude/settings.json` で、`.env` の読み書き、`git push origin main`、`gh pr merge` を拒否し、強制 push・`git reset --hard`・`git clean` は確認を求めます。公式ドキュメントにあるとおり、Bash のルールはコマンドの文字列で照合するため、別の書き方で回避できます。安全の境界ではなく、補助として扱ってください。
- 破壊的な操作（データの削除、main への push、PR のマージ、公開設定の変更、ライセンスの変更）は、利用者の承認なしには行いません（`CLAUDE.md`）。

## main のブランチ保護（推奨。まだ設定していません）

2026-10-29 時点で、`main` は `protected: false` でした（GitHub API の `list_branches` で確認）。次の設定を推奨します。設定するかどうかは利用者が決めます。

1. main への直接 push を禁止し、Pull Request を必須にする
2. 必須のステータスチェックとして CI の 6 ジョブ（`repo-safety`、`test` の 4 通り、`dependency-audit`）を指定する
3. 強制 push とブランチの削除を禁止する
4. （任意）1 人で運用するなら、承認の必須数は 0 のままにする。承認を必須にすると、自分の PR をマージできなくなります

## 依存関係の脆弱性検査

- `pip-audit` を `uv.lock` から書き出したハッシュ固定の一覧に対して実行します（CI の `dependency-audit` ジョブ）。
- 2026-10-29 にローカルで実行した結果は「No known vulnerabilities found」でした。これはその日の脆弱性データベースに基づく結果で、将来の安全を保証するものではありません。
