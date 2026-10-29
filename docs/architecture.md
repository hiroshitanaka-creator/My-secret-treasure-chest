# アーキテクチャ（Phase 0）

対象：`projects/ai-civilization-lab/src/civlab/`

## コンポーネントと責務

| コンポーネント | ファイル | 責務 | してはいけないこと |
| --- | --- | --- | --- |
| World Engine | `world.py` | 世界の状態（資源プール、住民）と規則。状態を変える**唯一の**場所。不変条件の検査と状態ハッシュ | 外部入力を検証なしで受け取ること |
| 行動の型と形式検証 | `actions.py` | 行動の種類、JSON スキーマ、`parse_action`（形式と、許可された行動かどうか） | 実行できるかどうかの判断（それは World Engine の役目） |
| Agent Interface | `agents.py` | 観察 → プロンプト → Budget Guard → Adapter（再試行の上限あり）→ 形式検証 → `Decision` | 状態を変更すること |
| Model Adapter | `adapters/base.py`, `mock.py`, `openai_adapter.py` | モデル呼び出し。生の文字列とトークン使用量を返す | 結果を解釈して状態に反映すること |
| プロンプト | `prompts.py` | 版付きのテンプレート（`resident-v0.1`）と、その指紋（ハッシュ） | 版を変えずに文面を変えること |
| Budget Guard | `budget.py` | 呼び出し回数・有料呼び出し回数・トークン・推定コストの上限、強制停止 | 実際の請求額を推定値で埋めること |
| Simulation Runner | `runner.py` | ターンの進行、各部の接続、実行情報（来歴）の収集、終了状態の記録 | 規則を判断すること |
| Experiment Store | `store.py` | SQLite。1 ターン＝1 トランザクション。自由記述は秘匿処理してから保存 | 書きかけのターンを残すこと |
| Evaluation | `evaluation.py` | 再生検証、要約指標、2 つの実行の比較 | — |
| 設定 | `config.py` | TOML の厳格な検証（未知のキーはエラー）。設定ハッシュとシナリオハッシュ | 秘密情報を読むこと |
| 秘匿処理 | `redaction.py` | ログと保存文字列から鍵の形をした文字列を伏せる | — |
| CLI | `cli.py` | `run` / `verify` / `summary` / `compare` / `list-runs` / `check-config` | — |

## 1 ターンのデータの流れ

```
WorldState(t) ──► build_observation ──► build_prompt(version)
                                              │
                         BudgetGuard.before_call（強制停止・上限の確認）
                                              │
                                    ModelAdapter.complete ──► 生の文字列 + トークン使用量
                                              │                （一時的な失敗は上限回数まで再試行）
                         BudgetGuard.after_call（回数・トークン・推定コストを加算）
                                              │
                                  parse_action（形式・許可された行動）
                                              │ 不正な出力 → action=None（記録だけする）
                                              ▼
WorldEngine.step(state, {住民: Action|None})
   ├─ 行動順：生存者を ID 順に並べ、ターンごとに回転させる
   ├─ 実行できるかの判定（在庫・対象の生死・量）。不可なら rejected_rule とし、状態は変えない
   ├─ 維持費：空腹と渇きが +1。閾値以上なら健康が減る。健康が 0 なら死亡
   ├─ 資源プールの回復（上限あり）
   └─ 不変条件の検査（負の値がない、上限内、alive と health が矛盾しない）
                                              ▼
Store.record_turn（状態・ハッシュ・行動・結果・API 呼び出しを 1 トランザクションで書く）
```

## 保存の構造（SQLite、スキーマ版 1）

- `experiments`：実験 ID と説明
- `runs`：run_id、状態（`running`/`completed`/`extinct`/`stopped_budget`/`stopped_kill_switch`/`interrupted`/`failed`）、開始と終了の時刻、データ区分（`split`：dev/heldout）、Adapter、モデル、プロンプトの版と指紋、シード、再現性区分、設定 JSON とそのハッシュ、シナリオハッシュ、来歴（civlab の版、Python、OS、Git コミット、未コミット変更の有無）、初期状態とそのハッシュ、完了ターン数、最終ハッシュ、予算の集計、エラー
- `turns`：ターンごとの状態 JSON・ハッシュ・死亡者（turn 0 は初期状態）
- `actions`：判断の状態（`valid`/`rejected_format`/`adapter_error`）、生の出力（秘匿処理済み）、検証済みの行動、World Engine での結果と効果
- `api_calls`：試行ごとの記録（有料かどうか、結果、トークン、使用量が API から報告されたか、推定コスト、実コスト（常に NULL））
- `errors`：エラーの種類とメッセージ（秘匿処理済み）

## 再現性の区分

| 区分 | 意味 | 該当するもの |
| --- | --- | --- |
| A | 完全に決定論的。同じ入力なら同じ出力 | World Engine の更新、Mock / Scripted Adapter |
| B | 条件は統制できるが、出力は変わりうる | OpenAI Adapter（LLM のサンプリング） |
| C | 制御も記録もしきれない外部状態に依存する | Phase 0 では該当なし（例：提供側のモデル更新、生データの取得） |

`runs.reproducibility_class` は Adapter が申告します。判断が B 区分であっても、記録した行動から世界の更新を再生する処理は常に A 区分です。そのため LLM を使った実行でも、`verify` で世界の更新を検算できます。

## 比較の公平性

- `scenario_hash`：世界の条件（人数・ターン数・シード・規則）だけのハッシュです。`compare` は、これが一致するときだけ `comparable: true` を返します。
- `config_hash`：設定全体のハッシュです。
- `split`（`dev` / `heldout`）：改善前後の比較で、開発に使う条件と、検証に取っておく条件を分けるための欄です。Phase 0 では記録するだけです。

## 重要な設計上の制約

1. モデルの出力だけでは、資源量や生死を決められません（`parse_action` を通ったうえで、World Engine の判定を経ます）。
2. 有料 API の呼び出しには 5 つの条件が必要です（[security.md](security.md)）。SDK の自動再試行は無効にしてあり、すべての試行を Budget Guard が数えます。
3. 失敗した呼び出しも、上限いっぱいのトークンを使ったものとして多めに数えます（課金されたかどうかは分からないため）。
4. 書き込みはターン単位でまとめて確定します。途中で失敗したり中断したりした場合、完了したターンだけが残り、実行の状態欄に理由が記録されます。終わらなかったターンで行った呼び出しは、`budget_json.unfinished_turn_calls` に残ります。
5. 規則や Mock の方針を変えると、`tests/test_simulation.py` で固定している最終ハッシュが変わります。意図した変更であれば、固定値を更新し、[decision-log.md](decision-log.md) に記録します。
