# Agent Context & System Rules: GitHub Copilot Docs Sync System

本リポジトリは、GitHub 公式ドキュメント（`github/docs` 内 `content/copilot/` 配下）を定期監視し、Google Docs API の Tabs 機能を介して Gemini Notebook（NotebookLM）参照用ドキュメント群へ自律同期する自動化基盤です。

作業にあたるすべての AI エージェントは、以下の前提・仕様・絶対的制約（Guardrails）に従ってコードの生成・編集・リファクタリングを行ってください。

---

## 1. 最重要ドキュメント（必読）

内部アルゴリズムや仕様の変更を行う前に、必ず以下の設計書を確認してください。

1. **`docs/System_Overview.md`**: 全体アーキテクチャ、全8分冊設計、フェイルセーフ方針
2. **`docs/Detailed_Logic.md`**: データ型、Liquid 解決パイプライン、正規表現、API トランザクション規約

---

## 2. 絶対的制約（Guardrails）

### ① 100 タブ All-or-Nothing フェイルセーフ

Google Docs API の 1 ドキュメントあたり最大 100 タブ制限を回避するため、Phase 1 実行前に以下を計算します。
$$\text{PredictedTabs} = \text{CurrentTabs} - \text{TabsToDelete} + \text{TabsToCreate}$$
`PredictedTabs > 100` となる場合は、**Google Docs API のリクエストを 1 件も送信せず即座に `RuntimeError` で停止（Abort）** してください。その際、コミット SHA キャッシュ（`last_commit_sha_*.txt`）を更新してはいけません。

### ② ナビゲーション用 `index.md` の完全除外

ファイル名が `index.md` で終わるファイルは、Web サイト用の Frontmatter（メタデータ）のみで本文が空です。差分抽出・全量同期の対象から**完全に除外（ブラックリスト化）** してください。

### ③ フラットマッピング & 50 文字制限

子タブ（Nested Tabs）は Gemini Notebook がインジェストできないため作成禁止です。すべて第 1 階層のフラットなタブとして作成し、パス区切りを `_` に置換します。50 文字を超える場合は `先頭41文字 + '_' + SHA-256先頭8文字` の計 50 文字に決定論的に短縮してください。

### ④ Liquid 3 段階解決パイプライン

Markdown 本文を Google Docs API に送信する前に、以下の順序で前処理を行います。

1. **Reusables 展開**: `{% data reusables.* %}` を `data/reusables/` 配下の Markdown で再帰展開（最大 3 階層）。
2. **UI・制御タグ平坦化**: `{% codetab python %}` や `{% vscode %}` を `#### Python` などの H4 見出しに変換。`ifversion` や不要アイコンタグを除去。
3. **Variables 辞書置換**: `{% data variables.* %}` を `data/variables/*.yml` の値で置換。

### ⑤ API 0 タブ禁止制約 & 作成先行順序

Google Docs API はドキュメント内に最低 1 つのタブが存在することを強制します。全入れ替え時であっても、**必ず先に `addDocumentTab` を実行し、その後に `deleteTab` を実行する順序** を保証してください。

### ⑥ 全削除防止ガード

手動全量同期（`--force`）時であっても、既存タブが 5 件以上存在し、全タブが孤立判定（削除対象）となった場合は異常系とみなして即座に処理を中断してください。

---

## 3. ディレクトリ構造

```text
├── .agents/
│   ├── AGENTS.md                  # 本ファイル
│   ├── rules/
│   │   └── coding-rules.md        # Python コーディング規約
│   └── skills/
│       ├── docs-tabs-logic/
│       │   └── SKILL.md           # Google Docs API タブ操作・Phase 1/Phase 2 同期仕様
│       ├── github-sync-logic/
│       │   └── SKILL.md           # GitHub 差分/全量取得・Liquid 3段階解決仕様
│       └── maintain-project-docs/
│           └── SKILL.md           # ドキュメント・スキル同期メンテナンス仕様
├── .github/
│   └── workflows/
│       └── sync.yml               # 日次同期 GitHub Actions ワークフロー
├── docs/
│   ├── System_Overview.md         # 全体アーキテクチャ・8分冊設計書
│   └── Detailed_Logic.md          # アルゴリズム・API 詳細設計書
├── src/
│   ├── config.py                  # 設定パーサー・検証
│   ├── docs_client.py             # Google Docs API クライアント（Phase 1 / Phase 2）
│   ├── github_client.py           # GitHub Compare API / 内容取得
│   ├── liquid_resolver.py         # Liquid 構文 3 段階解決エンジン
│   ├── main.py                    # 同期オーケストレーター
│   ├── models.py                  # データクラス・型定義
│   └── path_utils.py              # パス判定・フラットタブ命名ユーティリティ
├── tests/                         # 単体・結合テスト群
├── config.yaml                    # 監視フォルダ・Google Doc ID 定義
└── requirements.txt               # 依存ライブラリ
```
