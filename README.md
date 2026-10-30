# My-secret-treasure-chest

複数の実験プロジェクトを置くためのリポジトリです。現在のプロジェクトは 1 つです。

| プロジェクト | 場所 | 段階 |
| --- | --- | --- |
| AI文明進化実験室（AI Civilization Lab） | [`projects/ai-civilization-lab/`](projects/ai-civilization-lab/) | Phase 0：開発基盤 |

## AI文明進化実験室とは

将来つくるのは、次の 2 つを組み合わせたシステムです。

- **AI文明培養装置**：性格・役割・記憶を持つ AI 住民が仮想世界で暮らします。世界の状態は検証できるシミュレーションエンジンが更新します。
- **AI自己改善実験**：AI 教師が住民の失敗と成功を分析して判断ルールを改良します。同じ条件と未知の条件で改良前後を比べ、改善が確認できたときだけ「向上した」と判定します。

## 現在の実装状況（Phase 0）

| 機能 | 状態 |
| --- | --- |
| World Engine（食料・水、採集／飲食／共有／休息、健康と死亡） | 実装済み・テスト済み |
| Mock Adapter による住民 6 人×10 ターンの模擬実行 | 実装済み・テスト済み |
| SQLite への記録（実験・実行・各ターンの状態・行動・API 使用量・エラー） | 実装済み・テスト済み |
| 保存データからの再生検証（`civlab verify`）と改ざん検出 | 実装済み・テスト済み |
| Budget Guard（呼び出し回数・トークン・推定コストの上限、再試行の上限、強制停止） | 実装済み・テスト済み |
| OpenAI Responses API アダプター | 実装済み。**既定で無効**。実 SDK と偽の通信路でのみテスト済みで、実 API は未実行 |
| AI 教師・自己改善ループ、30 人以上・100 世代の実験 | 未実装（範囲外） |

模擬実行の結果は「基盤が動く」ことの確認であって、AI 文明が完成したことを示すものではありません。

## セットアップ

対応する Python は 3.11 以上です。CI では 3.11 と 3.14 を、Ubuntu と Windows で検査します。依存関係は `uv.lock` で固定しています。

### uv を使う方法（推奨・再現性あり）

macOS / Linux：

```bash
cd projects/ai-civilization-lab
uv sync --locked          # .venv を作り、uv.lock どおりに入れる
```

Windows（PowerShell）：

```powershell
py -3 --version                      # 3.11 以上であることを確認
py -m pip install --user uv          # uv が未導入のときだけ（ユーザー領域に入る）
cd projects\ai-civilization-lab
py -m uv sync --locked
```

`uv` コマンドに PATH が通っていれば、`py -m uv` は `uv` に置き換えられます。以下のコマンドも同じです。

### pip を使う方法（代替手段・版は固定されない）

pip 25.1 以上が必要です（`--group` を使うため）。

```bash
cd projects/ai-civilization-lab
python -m venv .venv
# Windows: .venv\Scripts\activate   macOS/Linux: source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e . --group dev
```

この方法では `uv.lock` が使われないため、入る依存パッケージの版が変わることがあります。

## 模擬シミュレーターの実行

`projects/ai-civilization-lab/` で次を実行します。

```bash
uv run civlab run --config configs/mock-6x10.toml --db runs/dev.sqlite3   # 6人×10ターン
uv run civlab verify --db runs/dev.sqlite3      # 最新の実行を再生し、全ターンのハッシュを照合
uv run civlab summary --db runs/dev.sqlite3     # 指標と API 使用量（paid_calls は 0）
uv run civlab list-runs --db runs/dev.sqlite3
uv run civlab compare --db runs/dev.sqlite3 <run_id_A> <run_id_B>
uv run civlab check-config --config configs/mock-6x10.toml
```

出力は JSON です。`runs/` と `*.sqlite3` は Git の管理対象外です。

## テストと静的解析

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
python ../../tools/check_repo_safety.py --history   # 秘密情報・禁止ファイルの検査（リポジトリ直下からなら tools/...）
```

テストは API キーなしで動き、テスト中の外部ネットワーク接続は遮断されます。

## 秘密情報の管理

- API キーは環境変数でだけ渡します。設定ファイルやソースコードには書きません。
- `.env` は Git の管理対象外です。`projects/ai-civilization-lab/.env.example` がひな形です。
- 有料 API は 5 つの条件（設定・環境変数・キー・予算・料金情報）がすべてそろわないと呼べません。詳しくは [docs/security.md](docs/security.md) を参照してください。

## ドキュメント

- [docs/architecture.md](docs/architecture.md)：構成、責務、データの流れ、設計上の制約
- [docs/decision-log.md](docs/decision-log.md)：技術選定とその理由、却下した案
- [docs/security.md](docs/security.md)：API キー、公開リポジトリ、権限、課金事故の防止
- [docs/phase0-plan.md](docs/phase0-plan.md)：実 API 実験へ進む条件、次の作業候補
- [docs/HANDOFF_TO_GPT.md](docs/HANDOFF_TO_GPT.md)：引継ぎ資料（実施内容・検証結果・未完了事項）
- [CLAUDE.md](CLAUDE.md)：Claude Code 向けの作業規則

## 今後の予定

[docs/phase0-plan.md](docs/phase0-plan.md) を参照してください。

## ライセンス

MIT（[LICENSE](LICENSE)）
