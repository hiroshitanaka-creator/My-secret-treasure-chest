# 引継ぎ資料：Phase 0 開発基盤 v0.1（ChatGPT と利用者による独立検証のために）

作成：Claude Code、2026-10-29（UTC）。この資料は実装者が書いた自己申告です。下の「検証の手順」で第三者が再確認できるようにしてあります。

## 1. 完了した作業

- `civlab` パッケージ（`projects/ai-civilization-lab/`）：World Engine、行動の検証、Agent Interface、Mock / Scripted / OpenAI の 3 種類の Adapter、Budget Guard、SQLite の Experiment Store、再生検証・要約・比較、CLI
- 住民 6 人×10 ターンの模擬実行（外部 API なし）、記録、再生検証
- テスト 175 件（テスト中はネットワーク遮断）、ruff、mypy strict
- GitHub Actions の CI：Ubuntu と Windows × Python 3.11/3.14、全履歴の安全検査、pip-audit
- `tools/check_repo_safety.py`（禁止ファイルと秘密情報の形をした文字列を、HEAD と全履歴から探す）
- `CLAUDE.md`、`.claude/settings.json`（deny / ask の最小限）、`.claude/rules/`、サブエージェント `independent-verifier`
- ドキュメント：README、architecture、decision-log、security、phase0-plan、この資料
- 独立検証（Claude Code のサブエージェント）で見つかった 6 件の修正

## 2. 未完了の作業と、判断が必要な事項

| 項目 | 状態 | 理由 | 次の対応（案） |
| --- | --- | --- | --- |
| PR のマージ | 未実施 | 依頼でマージは許可されていない | 利用者がレビューしてマージを判断する |
| main のブランチ保護 | 未設定 | 依頼で、利用者の承認後に行うとされている | [security.md](security.md) の推奨を承認するかどうか |
| 実 API での動作確認 | 未実施 | 今回の API 予算は 0 ドル | [phase0-plan.md](phase0-plan.md) の 7 条件を満たしてから |
| OpenAI 公式ドキュメントでの仕様確認 | 一部だけ | この作業環境からは developers.openai.com に接続できなかった（DNS 解決に失敗） | SDK の型定義では確認済み。実接続の前に公式ドキュメントで確認する |
| `.claude/settings.json` のスキーマ検証 | 未実施 | json.schemastore.org への接続がプロキシで拒否された（403） | 手元の Claude Code で `/status` を開き、`claude doctor` を実行して確認する。今回、`claude doctor`（2.1.312）は設定ファイルの問題を何も報告しなかったが、プロジェクトの設定ファイルまで調べたかどうかは出力に書かれていない |
| Windows での pip による代替手順 | 未検証 | CI は uv の手順だけを検査している | 必要なら CI に pip のジョブを足す |
| 利用者の手元（Windows）での動作 | 未検証 | 手元の環境には触れていない | README の手順で確認していただく |

## 3. 作成・変更したファイル

既存のファイルで変更したのは `README.md` だけです（1 行の見出しを全面的に拡充）。`LICENSE` は変更していません。

```
.claude/agents/independent-verifier.md      .claude/rules/api-cost.md
.claude/rules/verification.md               .claude/settings.json
.github/workflows/ci.yml                    .gitignore
CLAUDE.md                                   README.md（変更）
docs/architecture.md  docs/decision-log.md  docs/security.md
docs/phase0-plan.md   docs/HANDOFF_TO_GPT.md
tools/check_repo_safety.py
projects/ai-civilization-lab/
  .env.example  pyproject.toml  uv.lock  configs/mock-6x10.toml
  src/civlab/{__init__,__main__,actions,agents,budget,cli,config,evaluation,
              prompts,redaction,runner,store,world}.py
  src/civlab/adapters/{__init__,base,mock,openai_adapter}.py
  tests/{__init__,conftest,helpers}.py
  tests/test_{actions,budget,cli,config_and_redaction,openai_adapter,simulation,world}.py
```

## 4. 実行したテストと、その結果（実測）

### ローカル（Linux x86_64、cloud container）

| コマンド | 結果 |
| --- | --- |
| `uv sync --locked`（uv 0.13.6） | 成功 |
| `uv run ruff check .` / `ruff format --check .` | All checks passed / 27 files already formatted |
| `uv run mypy`（strict、src と tests） | Success: no issues found in 27 source files |
| `uv run pytest`（Python 3.13.17） | **175 passed** |
| 同じテストを Python 3.11.17 / 3.12.3 / 3.14.10 で | 175 passed / 175 passed / 175 passed |
| `civlab run --config configs/mock-6x10.toml` | completed、10 ターン、calls 60、paid_calls 0、estimated_cost_usd 0.0 |
| `civlab verify` | ok、turns_checked 10、sqlite_integrity ok |
| `python tools/check_repo_safety.py --history` | findings: 0 |
| 安全検査スクリプトの動作確認（作業用の別リポジトリに偽の鍵と `.env` を入れ、マージ解決のときだけ入るケースも試した） | 履歴から検出し、終了コード 1。値そのものは表示しない |
| pip-audit（ハッシュ固定の一覧、2026-10-29） | No known vulnerabilities found |
| actionlint 1.7.12 で `ci.yml` を検査 | エラー 0（shellcheck は入っていないため、その規則は無効） |
| pip による代替手順（Python 3.11、pip 26.2.2、`pip install -e . --group dev`） | 175 passed |

### 再現性

- 固定した最終ハッシュ（seed=7、6 人×10 ターン、Mock）：`84e64e794ff8fd9a8390b824a14e7e4ce6366753040d1938515dc577d68974d9`。Linux の Python 3.11/3.12/3.13/3.14 と、CI の 4 通りのテストで一致しました。
- `configs/mock-6x10.toml`（seed=20261030）の最終ハッシュ：`0739d8581efe22e803c27b0cc8429a00023b05634c76a4b2a13ed20b913c25e8`。手元の Linux と、CI の Windows / Python 3.11 のスモーク実行で一致しました。

## 5. CI の結果（GitHub Actions、実測）

| Run | コミット | 結果 | URL |
| --- | --- | --- | --- |
| #1 | 7767c20 | success（6 ジョブすべて） | https://github.com/hiroshitanaka-creator/My-secret-treasure-chest/actions/runs/40970180413 |
| #2 | 628f218 | success | https://github.com/hiroshitanaka-creator/My-secret-treasure-chest/actions/runs/40970643370 |
| #3 | 278470c | **failure**：4 つのテストジョブが ruff の RUF043（`pytest.raises` の `match=` に raw でない正規表現）で失敗。ruff をかけ直さずにテストを直してプッシュした、手順上の誤り | https://github.com/hiroshitanaka-creator/My-secret-treasure-chest/actions/runs/40971069682 |

Run #3 の失敗は、次のコミットで raw 文字列に直しました（手元で失敗を再現し、ruff・format・mypy・compileall・pytest 175 件・安全検査がすべて通ることを確かめています）。

Run #1 では、Windows / Python 3.11 のジョブのログで「166 passed」と、スモーク実行・verify の成功を確認しました。この資料自体を追加するコミットの CI は、この資料の中には書けません。最新の結果は PR #1 の Checks 欄で確認してください。

## 6. 証拠

- PR（Draft）：https://github.com/hiroshitanaka-creator/My-secret-treasure-chest/pull/1
- ブランチ：`claude/ai-civlab-foundation-v0.1`（base は `main` の ac0aa72）
- コミット：c9b3eec（パッケージ本体）→ 7767c20（CI・設定）→ 628f218（ドキュメント）→ 278470c（独立検証の指摘の修正）→ lint の修正と、この資料のコミット

## 7. 既知の問題と限界

- Mock の住民はルールで動くだけです。今回の結果は AI の知能や文明の性質を示しません。
- コストは推定値です。実際の請求額は取得していません（`actual_cost_usd` は常に NULL）。
- `verify` は DB の中の整合性を確かめるものです。設定・全状態・全ハッシュを矛盾なく書き換えられた場合は検出できません。外に残した最終ハッシュとの照合が必要です。
- `.claude/settings.json` の Bash ルールは、文字列で照合する補助的なものです（公式ドキュメントのとおり、回避できます）。
- 安全検査は自作の正規表現で行っています。gitleaks などの専用ツールほど網羅的ではありません。
- テストが遮断しているのは Python の `socket` を経由する接続です。それ以外の経路による通信までは保証しません。

## 8. 独立検証の結果（Stage F）

Claude Code の汎用サブエージェントに、実装者の申告を信用せずに再検証させました（読み取りだけ。作業用ディレクトリだけに書き込み）。主張 8 項目はすべて「確認」でした。指摘は次の 6 件で、すべて 278470c で修正し、回帰テストを足しました。

1. 中：Python 3.11 で深く入れ子にした JSON を受けると `RecursionError` が出て、実行全体が失敗扱いになる → 形式エラーとして扱うようにした
2. 低〜中：料金 0 が「設定済み」と扱われ、コスト上限が効かない → 0 より大きい値を必須にした
3. 低：想定外の例外が起きたとき、有料呼び出しが計上されない → 多めに計上してから停止する。事前の見積りに JSON スキーマの分も含めた
4. 低：verify が設定の改ざんを検出しない。turn 0 を作り直して確かめていない。壊れた行で例外が出る → すべて対応した
5. 低：実行の開始と turn 0 の書き込みが別々のトランザクション。中断のタイミングによって最終ハッシュがずれる → 1 つのトランザクションにまとめ、最終ハッシュは保存済みの最後のターンから取るようにした
6. 低：履歴の走査がマージコミットを見ていない → `-m` を付けた

同じサブエージェントによる修正後の再検証は行っていません。修正の確認は、回帰テストと CI だけで行っています。

## 9. 次に判断が必要な事項（優先順の案）

1. PR #1 をレビューし、マージするかどうか
2. main のブランチ保護を設定するかどうか（[security.md](security.md)）
3. Phase 1 の最初の実 API 実験の範囲・上限額・モデル（[phase0-plan.md](phase0-plan.md) の条件 1〜7）
4. 自作の安全検査に加えて gitleaks などを導入するかどうか
5. 2 つ目のプロジェクトを追加するときに、docs をプロジェクト配下へ移すかどうか

## 10. 検証の手順（第三者向け）

```bash
git clone https://github.com/hiroshitanaka-creator/My-secret-treasure-chest
cd My-secret-treasure-chest && git checkout claude/ai-civlab-foundation-v0.1
python tools/check_repo_safety.py --history
cd projects/ai-civilization-lab
uv sync --locked && uv run ruff check . && uv run mypy && uv run pytest
uv run civlab run --config configs/mock-6x10.toml --db runs/check.sqlite3
uv run civlab verify --db runs/check.sqlite3      # ok: true になり、最終ハッシュが 0739d858… であること
```
