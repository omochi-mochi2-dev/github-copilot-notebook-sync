---
name: github-sync-logic
description: >
  GitHub API（Compare API / Git Trees API）からの差分・全量ファイル取得、ナビゲーション用 index.md の除外判定、
  Frontmatter 整形、および Liquid 3 段階解決パイプライン（Reusables 展開・UI/制御タグ平坦化・Variables 辞書置換）の
  実装・修正・レビュー時に使用。
---

# Skill: GitHub Fetch & Content Resolution Logic (GitHub Copilot Docs)

本スキルは、`github/docs` リポジトリ（`content/copilot/` 配下）からのファイル変更取得、フィルタリング、および Liquid テンプレート解決パイプラインの実装仕様を定義する。

---

## 1. ファイル抽出・フィルタリング規則

### ① 対象拡張子

- `.md` および `.txt` のみを対象とする。

### ② index.md の完全除外（最重要）

- `content/copilot/` 配下の各ディレクトリに存在する `index.md` は、Web サイトのナビゲーション Frontmatter のみで本文が存在しない。
- Gemini Notebook に同期しても検索ノイズおよびタブ枠の浪費となるため、以下の条件で完全に除外する。
  ```python
  def is_valid_sync_file(filename: str) -> bool:
      if not (filename.endswith(".md") or filename.endswith(".txt")):
          return False
      if filename == "index.md" or filename.endswith("/index.md") or filename.endswith("\\index.md"):
          return False
      return True
  ```

---

## 2. Liquid 3 段階解決パイプライン (`liquid_resolver.py`)

GitHub Docs は Liquid テンプレートに強く依存しているため、Google Docs API に送信する前に Frontmatter を `# {title}\n\n{intro}` に整形し、`{% raw %}...{% endraw %}` ブロックを保護したうえで以下の順序でテキストを正規化する。

### Step 1: Reusables 展開 (`data/reusables/`)

- 構文: `{% data reusables.<path>.<file> %}`（`{%- ... -%}` および `indented_data_reference` を含む）
- 展開前に `{% comment %}...{% endcomment %}` ブロックを先行除去し、コメント内の無効参照を読み込まないようにする。
- `data/reusables/` 配下の Markdown 本文を展開する。
- スニペット内にさらに Reusable が含まれる場合があるため、最大 3 階層まで再帰展開する（循環参照は即時エラー）。

### Step 2: UI タグ・バージョン分岐の平坦化

Gemini Notebook が理解しやすいよう、Web 表示用タグを Markdown 見出しや記号へ変換・除去する。

- `{% codetab <lang> %}` → `\n\n#### <Lang名>\n\n`
- `{% vscode %}`, `{% jetbrains %}`, `{% visualstudio %}` 等 → `\n\n#### <ツール名>\n\n`
- `{% codetabs %}`, `{% endcodetabs %}`, `{% endvscode %}` 等 → 除去
- `{% ifversion ... %}`, `{% endif %}`, `{% else %}` → 除去（全バージョン知識を保持）
- `{% octicon "check" %}` → `✓`（その他 octicon は除去）
- `{% comment %}...{% endcomment %}` → ブロックごと除去

### Step 3: Variables 辞書置換 (`data/variables/`)

- 構文: `{% data variables.<file>.<key> %}`（パターン `r'\{%-?\s*data\s+variables\.([\w-]+)\.([\w.-]+)\s*-?%\}'` により、`3.8` などドットを含むキー名にも対応）
- `data/variables/*.yml` をコミット SHA 単位でキャッシュロードし、該当文字列に一括置換する。
- 変数値自体に Liquid（`ifversion` や他変数参照）が含まれる場合も、最大 3 階層まで再帰解決する。
- 該当キーが存在しない場合は、タグを除去してキー名自体を残す。

---

## 3. GitHub API クライアント仕様 (`github_client.py`)

- **差分同期**: GitHub Compare API (`repos/{owner}/{repo}/compare/{base}...{head}`) を使用し、監視対象フォルダ内の変更ファイル（added, modified, removed, renamed）を抽出する。
- **全量同期フォールバック**: Compare API が 300 ファイル上限に到達した場合（`truncated`）、または `data/reusables/`・`data/variables/` 配下の変更が検知された場合は、差分の取りこぼしや変数更新漏れを防ぐため自動的に全量同期へ切り替える。
- **全量同期 (`--force`)**: 監視フォルダのツリー SHA を特定し、サブツリーのみを Git Trees API (`recursive=1`) で取得する（監視フォルダ未検出時は即時エラー停止）。
- **通信耐性**: 一時的な切断（`RemoteDisconnected`）やレート制限・サーバーエラー（HTTP 429 / 5xx）に備え、urllib3 の `Retry(total=3, backoff_factor=1)` を適用したセッションを使用する。
