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

GitHub Docs は Liquid テンプレートに強く依存しているため、Google Docs API に送信する前に以下の順序でテキストを正規化する。

### Step 1: Reusables 展開 (`data/reusables/`)

- 構文: `{% data reusables.<path>.<file> %}`
- `data/reusables/` 配下の Markdown 本文を展開する。
- スニペット内にさらに Reusable が含まれる場合があるため、最大 3 階層まで再帰展開する。

### Step 2: UI タグ・バージョン分岐の平坦化

Gemini Notebook が理解しやすいよう、Web 表示用タグを Markdown 見出しや記号へ変換・除去する。

- `{% codetab <lang> %}` → `\n\n#### <Lang名>\n\n`
- `{% vscode %}`, `{% jetbrains %}`, `{% visualstudio %}` 等 → `\n\n#### <ツール名>\n\n`
- `{% codetabs %}`, `{% endcodetabs %}`, `{% endvscode %}` 等 → 除去
- `{% ifversion ... %}`, `{% endif %}`, `{% else %}` → 除去（全バージョン知識を保持）
- `{% octicon "check" %}` → `✓`（その他 octicon は除去）
- `{% comment %}...{% endcomment %}` → ブロックごと除去

### Step 3: Variables 辞書置換 (`data/variables/`)

- 構文: `{% data variables.<file>.<key> %}`
- `data/variables/*.yml` をキャッシュロードし、該当文字列に一括置換する。
- 該当キーが存在しない場合は、タグを除去してキー名自体を残す。

---

## 3. GitHub API クライアント仕様 (`github_client.py`)

- **差分同期**: GitHub Compare API (`repos/{owner}/{repo}/compare/{base}...{head}`) を使用し、監視対象フォルダ内の変更ファイル（added, modified, removed, renamed）を抽出する。
- **全量同期 (`--force`)**: GitHub Git Trees API (`recursive=1`) または Contents API を使用し、対象フォルダ配下の全有効 Markdown を `SyncAction.ADD` として取得する。
- **通信耐性**: 一時的な切断（`RemoteDisconnected`）やレート制限に備え、urllib3 の `Retry(total=3, backoff_factor=1)` を適用したセッションを使用する。
