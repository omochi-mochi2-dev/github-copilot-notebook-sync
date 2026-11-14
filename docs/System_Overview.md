# System Overview: GitHub Copilot Docs ↔ Google Docs Sync System

## 1. システム目的と概要

本システムは、GitHub 公式ドキュメントリポジトリ（`github/docs`）内の GitHub Copilot 関連ドキュメント（`content/copilot/` 配下）の更新・差分を自動検知し、Google Docs API の Tabs 機能を経由して Google ドキュメント（Gemini Notebook / 旧 NotebookLM 参照用）へ自律同期するシステムです。

膨大なドキュメント群を業務・関心事ごとのテーマ（全8分冊設計）に分割管理することで、Google Docs API の「1ドキュメント最大100タブ」制限を完全に回避し、Gemini Notebook 上での RAG（検索拡張生成）の参照精度とコンテキスト引用（Citations）の正確性を最大化します。

---

## 2. システムアーキテクチャ方針

- **実行環境**: GitHub Actions（スケジュールトリガーによる日次実行 `17 2 * * *`（02:17 UTC / 11:17 JST）、および `workflow_dispatch` による手動強制同期 `--force` 対応）
- **差分検出**: `actions/cache` を用いて前回の同期コミット SHA（`last_commit_sha_{owner}_{repo}.txt`）を永続化し、GitHub Compare API で差分ファイルのみを抽出するステートフル同期
- **ターゲット構成**: 1 つの監視対象クラスタ（複数の関連フォルダ）に対して 1 つの Google ドキュメントを割り当て、配下の Markdown ファイルを第一階層のタブ（Tabs）として同期

---

## 3. AI エージェントへの絶対的制約 (Guardrails) & データ変換規則

### ① フラットマッピング戦略（Flat Mapping Strategy）

- **制約**: Google Docs 上で子タブ（ネストされたタブ構造）は**絶対に作成しないこと**。
- **理由**: Gemini Notebook のインジェスト仕様上、子タブ内のコンテンツは RAG 解析時に無視されるため。
- **命名規則**: ディレクトリの相対パス構造（例: `concepts/agent-capabilities/about-agents/agent-apps.md`）はスラッシュおよびドットを置換したフラットな第 1 階層のタブ名（例: `concepts_agent-capabilities_about-agents_agent-apps_md`）として作成する。
- **50文字上限と重複防止**: Google Docs API のタブタイトル長上限（最大 50 文字）を満たすため、50 文字を超えるパス名は `先頭41文字 + '_' + SHA-256ハッシュ先頭8文字` の計 50 文字に決定論的に短縮し、名前衝突（コリジョン）を 100% 防止する。

### ② ナビゲーションファイル（`index.md`）の完全除外

- **制約**: ファイル名が `index.md` で終わるファイルは、差分抽出および同期対象から**完全に除外（ブラックリスト化）すること**。
- **理由**: `github/docs` の各ディレクトリにある `index.md` は、Web サイトのルーティングと Frontmatter（メタデータ）のみで構成されており、本文（Markdown body）が存在しない。これを同期すると Gemini Notebook の検索ノイズになるだけでなく、貴重な 100 タブ枠を無駄に圧迫するため。

### ③ Liquid 構文の解決と平坦化（Reusables / UIタグ / Variables）

- **制約**: Markdown 本文を Google Docs API に送信する前処理として、以下の 3 段階解決を必ず適用すること。
  1. **Reusables（共通スニペット）展開**: `{% data reusables.* %}` を検知し、`data/reusables/` 配下の該当 Markdown 本文でインライン置換（最大 3 階層まで再帰展開）する。未解決のまま放置して本文手順が欠落する事故を物理的に防ぐこと。
  2. **UI・バージョンタグの平坦化**: 
     - コード・エディタ切替（`{% codetab python %}`, `{% vscode %}` 等）は `#### Python` などの H4 見出しテキストに変換し、タブ内のコードや説明をすべて本文として残す。
     - バージョン分岐（`{% ifversion ... %}`, `{% endif %}` 等）や装飾枠（`{% rowheaders %}` 等）はタグ自体を除去して中の説明文を保持する。
     - アイコンタグ（`{% octicon "check" %}` ➔ `✓`）を置換し、コメント（`{% comment %}` ブロック）は丸ごと除去する。
  3. **Variables（固有名詞変数）置換**: `{% data variables.* %}` を `data/variables/*.yml`（`product.yml`, `copilot.yml` 等）の値で一括置換する。
- **理由**: 手順文の欠落を防ぎ、Web 用の表示制御タグを平坦なテキスト構造に整えることで、Gemini Notebook の検索性と回答生成精度を最高水準に保つため。

---

## 4. バッチ更新とトランザクション設計

### ① 二段階同期（Two-Step Sync Pipeline）

Google Docs API では新規作成される `tabId` が動的に発行されるため、同一の `batchUpdate` 内でタブ作成とテキスト挿入を同時に行うことはできない。そのため、以下の二段階パイプラインを厳格に順守する:

1. **Phase 1（構造同期）**: `addDocumentTab` による新規タブ作成、`deleteTab` による不要・旧タブ削除を実行。
2. **Re-fetch**: `documents.get(includeTabsContent=True)` で最新のドキュメント構造（最新 `tabId` マップおよび各タブの `endIndex`）を再取得。
3. **Phase 2（コンテンツ同期）**: 対象タブに対し、`deleteContentRange`（既存テキスト全消去）と `insertText`（最新 Markdown 本文挿入）をペアで実行。

### ② Phase 2 チャンク分割（Socket Timeout 対策）

一度に大量のリクエストを送信して Google Docs API の 60 秒ソケットタイムアウトが発生するのを防ぐため、Phase 2 のリクエスト配列は `tabId` 単位のアトミック性を維持したまま、最大 10 タブ（20 リクエスト）ごとにチャンク分割して順次送信すること。

### ③ 孤立タブ（Orphan Tabs）の自律クリーンアップ

通常時のファイル削除（`status == 'removed'`）やファイルリネーム時（`status == 'renamed'`）に加え、手動全量同期（`--force`）時においても、Google Docs 側に存在するタブ名と GitHub 側の有効ファイル一覧を突き合わせ、リポジトリから消失した孤立タブを自動的に `deleteTab` でクリーンアップすること。

---

## 5. 自己修復・フォールトトレランス設計

### ① 100 タブ All-or-Nothing フェイルセーフ

- `build_phase1_plan` において、追加・削除・リネームを加味した最終タブ予測数が 100 件を超える場合は、**1 件も Google Docs を変更せずに即座に `RuntimeError` で処理を中断（Abort）すること**。
- 中断時はコミット SHA キャッシュ（`last_commit_sha_*.txt`）を絶対に更新せず、次回の実行へ差分状態を温存すること（部分同期によるサイレントデータロストの防止）。

### ② 完全消失時の全削除防止ガード

抽出された差分がすべて `status == 'removed'` の場合、または誤動作により既存の全タブが削除対象と判定された場合は、異常事態とみなして即座に処理をブロックし、Google ドキュメントが空になる事故を物理的に防ぐこと。

### ③ ファイル移動・リネームの自動分類と監視パス消失時の安全保護

- GitHub Compare API の `status: "renamed"` および `previous_filename` を監視フォルダ群（分冊単位）にスコープして解析し、監視フォルダ外への移動は `removed`、監視フォルダ外からの移動は `added`、同一分冊内の移動は旧タブ削除＋新タブ作成として自動的に追従する。
- 全量同期時に対象の監視フォルダ自体がリポジトリ上から消失（未検出）した場合は、空リストとして全タブを孤立削除してしまう事故を防ぐため、即座にエラーで処理を停止し既存タブを保護する。

### ④ 異常時の管理者アラート

フェイルセーフ発動や同期エラー発生時は、GitHub Actions の実行をエラー終了（Exit 1）させ、GitHub Repository へ Issue を自動起票して管理者に緊急通知する。

---

## 6. ドキュメント分割クラスタリング（全体ロードマップ・確定版8分冊）

実ファイル数（`index.md` を除外した有効な `.md` ファイル数）の実測値に基づき、以下の 8 分冊体制で運用する。全クラスタが Google Docs API の 100 タブ上限を下回り、十分なヘッドルームを確保していることを検証済み。

| 分冊番号 | ドキュメント名（役割） | 対象ディレクトリパス | 有効ファイル数 | ヘッドルーム（余裕） |
| :--- | :--- | :--- | :--- | :--- |
| **Doc 1** | **Concepts & Getting Started**（稼働中） | `get-started/`, `concepts/`, `responsible-use/`, `copilot-on-ghes/` | 85 件 | 15 件 |
| **Doc 2** | **CLI & Terminal Workflows**（稼働中） | `how-tos/copilot-cli/`, `how-tos/github-agentic-workflows/` | 45 件 | 55 件 |
| **Doc 3** | **Surfaces & Interactive Coding**（稼働中） | `how-tos/copilot-in-your-ide/`, `how-tos/copilot-on-github/`, `how-tos/github-copilot-app/` | 74 件 | 26 件 |
| **Doc 4** | **Autonomous Agents & SDK**（稼働中） | `how-tos/copilot-sdk/`, `how-tos/use-copilot-agents/`, `how-tos/copilot-integrations/` | 67 件 | 33 件 |
| **Doc 5** | **Enterprise Administration & Security**（稼働中） | `how-tos/administer-copilot/`, `how-tos/cloud-and-local-sandboxes/`, `how-tos/configure-content-exclusion/`, `how-tos/manage-and-track-spending/`, `how-tos/manage-your-account/`, `how-tos/troubleshoot-copilot/` | 61 件 | 39 件 |
| **Doc 6** | **Cookbook & Customization**（稼働中） | `tutorials/copilot-cookbook/`, `tutorials/customization-library/` | 43 件 | 57 件 |
| **Doc 7** | **Tutorials, Adoption & Scale**（稼働中） | `tutorials/` 直下, `tutorials/budgets/`, `tutorials/cloud-agent/`, `tutorials/roll-out-at-scale/` | 44 件 | 56 件 |
| **Doc 8** | **Technical Reference** | `reference/` | 48 件 | 52 件 |