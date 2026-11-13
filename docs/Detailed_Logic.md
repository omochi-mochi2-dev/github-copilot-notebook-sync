# Detailed Logic & Algorithmic Specifications: GitHub Copilot Docs ↔ Google Docs Sync

本ドキュメントは、GitHub Copilot 公式ドキュメント（`github/docs` 内 `content/copilot/`）を Google Docs Tabs に自律同期するシステムの内部アルゴリズム、データ構造、正規表現、および API トランザクション仕様を定義する詳細設計書である。

---

## 1. データモデル設計（Data Models & Schema）

`src/models.py` において以下の型定義を行い、パイプライン全域で厳格な型安全性を保証する。

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any

class SyncAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NOOP = "NOOP"

@dataclass
class FileChange:
    """GitHub 差分または全量探索から抽出された単一ファイル変更情報"""
    path: str                       # 例: "content/copilot/concepts/agent-concepts.md"
    status: str                     # "added", "modified", "removed", "renamed"
    raw_content: Optional[str] = None
    resolved_content: Optional[str] = None
    previous_path: Optional[str] = None  # status == 'renamed' の場合のみ設定

@dataclass
class DocumentTabState:
    """Google Docs 側の既存タブ状態"""
    tab_id: str                     # 例: "t.1234567890"
    title: str                      # 例: "concepts_agent-concepts_md"
    end_index: int                  # タブ本文の終了インデックス（文字数 + 1）

@dataclass
class WatchFolderConfig:
    """config.yaml の watch_folders 定義"""
    source_path: str
    google_doc_id: str
    doc_name: str

@dataclass
class SyncTargetConfig:
    """config.yaml の sync_targets 定義"""
    repository: str
    watch_folders: List[WatchFolderConfig]

@dataclass
class Phase1Plan:
    """Phase 1: 構造同期実行計画"""
    google_doc_id: str
    tabs_to_create: List[str]       # 作成するタブ名（フラット化済み）
    tabs_to_delete: List[str]       # 削除するタブID
    predicted_tab_count: int

@dataclass
class Phase2TabUpdate:
    """Phase 2: 単一タブ更新ペイロード"""
    tab_id: str
    tab_title: str
    target_end_index: int
    content_to_insert: str

@dataclass
class Phase2Plan:
    """Phase 2: コンテンツ同期実行計画"""
    google_doc_id: str
    updates: List[Phase2TabUpdate]

```

---

## 2. ファイル抽出とフィルタリング仕様（GitHub Client）

### ① 対象ファイル検証ロジック (`is_valid_sync_file`)

GitHub Docs 内の各ディレクトリに配置されている `index.md` は、本文が空でありサイトルーティング用のメタデータ（YAML Frontmatter）のみで構成されているため、同期対象から完全に除外する。

```python
def is_valid_sync_file(filename: str) -> bool:
    """
    同期対象とする Markdown ファイルかを判定する。
    - 拡張子が .md または .txt であること。
    - ナビゲーション用の index.md は完全除外する。
    """
    # 1. 拡張子チェック
    if not (filename.endswith(".md") or filename.endswith(".txt")):
        return False

    # 2. index.md の完全除外（末尾一致またはパス境界一致）
    if filename == "index.md" or filename.endswith("/index.md") or filename.endswith("\\index.md"):
        return False

    return True

```

### ② フォルダ所属判定ロジック (`is_under_watch_folder`)

```python
def is_under_watch_folder(file_path: str, watch_folder: str) -> bool:
    normalized_path = file_path.replace("\\", "/").strip("/")
    normalized_folder = watch_folder.replace("\\", "/").strip("/")
    return normalized_path.startswith(normalized_folder + "/")

```

### ③ 全量同期への自動フォールバック条件

通常の差分同期（Compare API）実行時であっても、以下のいずれかに該当する場合は差分の取りこぼしやスニペット・変数更新の未反映を防ぐため、自動的に全量同期モード（`effective_force = True`）へ切り替える。

- **Compare API の 300 ファイル上限到達**: `files` 配列が 300 件に達し、差分が切り詰められた（`truncated`）可能性がある場合。
- **`data/` 配下の変更検知**: コミット差分に `data/reusables/` または `data/variables/` 配下のファイル変更が含まれており、Markdown ファイル自体に変更がなくても解決後の本文が変化し得る場合。
- **監視フォルダのサブツリー取得**: 全量同期時は `github/docs` ルートツリーの `truncated` を回避するため、監視フォルダのツリー SHA を特定したうえでサブツリーのみを `git/trees/{sha}?recursive=1` で取得する。監視フォルダ自体が取得できない場合は、全タブの孤立判定事故を防ぐため即座に例外で停止する。

---

## 3. パス平坦化とタブ命名アルゴリズム（Tab Naming Strategy）

Gemini Notebook（旧 NotebookLM）はネストされた子タブをインジェストできないため、すべてのファイルを第 1 階層のフラットなタブとして登録する。Google Docs API のタブ名最大 50 文字制限を満たしつつ、名前衝突を決定論的に防止する。

### 命名仕様

1. **記号置換**: 監視フォルダからの相対パスに対し、`/`、`\`、`.` をすべて `_` に置換する。
2. **長さ判定**:

- `len(flat_name) <= 50`: そのまま採用。
- `len(flat_name) > 50`: `先頭41文字 + "_" + SHA-256ハッシュ先頭8文字` の計 50 文字に短縮。

```python
import hashlib

def generate_flat_tab_title(relative_path: str) -> str:
    """
    相対パスから Google Docs タブ名を一意かつ安全に生成する（最大50文字）。
    例: 'concepts/agent-capabilities/about-agents.md'
        -> 'concepts_agent-capabilities_about-agents_md' (43文字)
    """
    flat = relative_path.replace("/", "_").replace("\\", "_").replace(".", "_")

    if len(flat) <= 50:
        return flat

    hash_digest = hashlib.sha256(flat.encode("utf-8")).hexdigest()[:8]
    return f"{flat[:41]}_{hash_digest}"

```

---

## 4. Liquid 構文解決・平坦化パイプライン（Liquid Resolver Engine）

GitHub Copilot ドキュメントの本文を Gemini Notebook が最高精度で解析できるよう、API 送信前に以下の 3 段階処理を順次実行する。

```text
Raw Markdown
  │
  ▼ 【Phase A: Reusables 展開】（共通手順スニペットのインライン展開）
  │
  ▼ 【Phase B: UI & 制御タグ平坦化】（codetab, IDEタグを見出し化、不要タグ除去）
  │
  ▼ 【Phase C: Variables 辞書置換】（product.yml / copilot.yml 等の変数置換）
  │
Clean & Flattened Markdown (Google Docs API へ送信)

```

### ⓪ 前処理: Frontmatter 整形 & `{% raw %}` ブロック保護

- **Frontmatter 整形**: YAML Frontmatter から `title` と `intro` のみを抽出し、本文先頭に `# {title}\n\n{intro}` として配置したうえで残りのメタデータ（`versions`, `redirect_from` 等）を除去する。
- **`{% raw %}...{% endraw %}` の退避と復元**: GitHub Actions ワークフロー例（`${{ github.sha }}` 等）やテンプレート例示を破壊しないよう、Phase A 実行前にプレースホルダーへ退避し、全段階完了後にそのまま復元する。

### ① Phase A: Reusables 展開仕様

`{% data reusables.* %}`（空白制御記法 `{%- ... -%}` および `indented_data_reference` を含む）を検知し、リポジトリの `data/reusables/` 配下の実 Markdown ファイル本文で再帰置換する。

- **コメントブロックの先行除去**: コメントアウトされた無効な参照を無駄に取得しないよう、展開前に `{% comment %}[\s\S]*?{% endcomment %}` を先行除去する。
- **構文パターン**: `r'\{%-?\s*data\s+reusables\.([^\s%]+)\s*-?%\}'`
- **ファイル解決規則**: `reusables.copilot.mcp.about-github-mcp-server` ➔ `data/reusables/copilot/mcp/about-github-mcp-server.md`
- **再帰展開制限**: スニペット内に別のスニペットが含まれるケースに対応するため、最大 **3 階層** まで展開する。循環参照は即座にエラーとする。

### ② Phase B: UI・バージョンタグ平坦化仕様

Web 表示用の分岐やタブUIタグを、Gemini Notebook が理解しやすい階層見出しやプレーンテキストに正規化する。

| 対象タグ構文                                                 | 変換規則                   | 変換理由                               |
| ------------------------------------------------------------ | -------------------------- | -------------------------------------- |
| `{% codetabs %}` / `{% endcodetabs %}`                       | 空文字（除去）             | タブUIコンテナの除去                   |
| `{% codetab <lang> %}`                                       | `\n\n#### <Lang名>\n\n`    | 言語切り替えを見出し化してコードを保持 |
| `{% endcodetab %}`                                           | `\n`                       | 終端改行                               |
| `{% vscode %}` / `{% jetbrains %}` / `{% visualstudio %}` 等 | `\n\n#### <ツール名>\n\n`  | IDE 別手順を見出し化して全手順を保持   |
| `{% endvscode %}` 等                                         | `\n`                       | 終端改行                               |
| `{% ifversion ... %}` / `{% endif %}` / `{% else %}`         | 空文字（除去）             | バージョン差異によらず全知識を学習     |
| `{% rowheaders %}` / `{% endrowheaders %}` / `{% prompt %}` 等 | 空文字（タグのみ除去）   | 表装飾・プロンプト装飾タグの平坦化     |
| `{% octicon "check" %}`                                      | `✓`                        | 記号への置き換え                       |
| `{% octicon "<その他>" %}`                                   | 空文字（除去）             | 装飾アイコンのノイズ除去               |
| `{% comment %}[\s\S]*?{% endcomment %}`                      | 空文字（ブロックごと除去） | コメントアウトの消去                   |

### ③ Phase C: Variables 辞書置換仕様

実測調査に基づき、すべての変数が 2 階層（`data variables.<ファイル名>.<キー>`）であることを利用して置換する。

- **構文パターン**: `r'\{%-?\s*data\s+variables\.([\w-]+)\.([\w.-]+)\s*-?%\}'`（バージョン番号 `3.8` などドットを含む YAML キー名にも対応）
- **データソース**: `data/variables/*.yml`（`product.yml`, `copilot.yml`, `release-phases.yml` など）
- **データ構造**: 各 YAML は単純なフラット辞書（`Dict[str, str]`）としてコミット SHA 単位でキャッシュロード。
- **変数値内 Liquid の再帰解決**: 変数の値自体に `{% ifversion %}` 分岐や他変数への参照が含まれる場合があるため、変数値に対しても Phase B の平坦化および最大 **3 階層** までの変数再帰置換を適用する。
- **フォールバック**: 未定義キーの場合は、タグを除去してキー名自体をそのまま残す（例: `prodname_copilot`）。

---

## 5. Google Docs API トランザクション・バッチ処理仕様

Google Docs API の仕様上、新規タブ作成（`addDocumentTab`）とテキスト挿入（`insertText`）は同一の `batchUpdate` では実行できないため、厳格な二段階パイプライン（Two-Step Pipeline）を適用する。

### ① Phase 1: 構造同期（Structure Sync）

1. 対象 Google ドキュメントの最新状態を `documents.get(includeTabsContent=True)` で取得する（※ Google Docs API の仕様上、`includeTabsContent=False` では `tabs` フィールドが空となり既存タブ一覧を取得できないため、Phase 1 前の取得から必ず `includeTabsContent=True` を指定する）。
2. 既存タブ一覧と、今回の GitHub 差分・同期対象一覧を照合。
3. **100 タブ All-or-Nothing フェイルセーフ判定**:

$$\text{PredictedTabs} = \text{CurrentTabs} - \text{TabsToDelete} + \text{TabsToCreate}$$

$\text{PredictedTabs} > 100$ の場合は、**Google Docs API のリクエストを 1 件も発行せず、即座に `RuntimeError` で停止（Abort）する**。コミット SHA キャッシュも更新しない。4. **全削除防止ガード（Total Deletion Protection）**:
既存タブが 5 件以上存在し、`len(tabs_to_delete) >= len(current_tabs)`（または削除後にタブが 0 件になる）判定となった場合は、API の異常や比較不良とみなして即座に `RuntimeError` で Abort する。5. **作成先行順序保証（API 0 タブ禁止制約対策）**:
Google Docs API は「ドキュメント内に最低 1 つのタブが存在すること」を強制する。そのため、全入れ替えが発生する場合であっても一時的にタブが 0 個になるエラー（400 Bad Request）を防ぐため、**リクエスト配列内では必ず先に `addDocumentTab` を実行し、その後に `deleteTab` を実行する** 順序を物理的に保証する。

### ② Re-fetch

Phase 1 実行後、`documents.get(includeTabsContent=True)` を実行し、新規発行された `tabId` マップと各タブの `endIndex` を取得する。

### ③ Phase 2: コンテンツ同期（Content Sync）

1. 各対象タブについて、以下の 2 リクエストをペアで生成する。

- **Clear Request**:

```json
{
  "deleteContentRange": {
    "range": {
      "tabId": "<TAB_ID>",
      "segmentId": "",
      "startIndex": 1,
      "endIndex": "<TAB_END_INDEX - 1>"
    }
  }
}
```

※ `endIndex` が 2 以下の空タブの場合はクリアをスキップする。

- **Insert Request**:

```json
{
  "insertText": {
    "location": {
      "tabId": "<TAB_ID>",
      "segmentId": "",
      "index": 1
    },
    "text": "<RESOLVED_MARKDOWN_BODY>"
  }
}
```

2. **Phase 2 チャンク分割送信（Socket Timeout 対策）**:
   一度に大量のタブ（テキスト）を送信すると Google Docs API の 60 秒ソケットタイムアウトが発生するため、**最大 10 タブ（20 リクエスト）単位** でバッチを分割し、順次 `batchUpdate` を呼び出す。

---

## 6. 孤立タブ（Orphan Tabs）の自律クリーンアップ仕様

クリーンアップは誤削除を物理的に排除するため、実行モードに応じて以下の通り厳格に分離する。

### ① 通常差分同期時（Daily Sync）

- GitHub Compare API の解析結果、明示的に `status == 'removed'` または `status == 'renamed'`（旧パス側）となったファイルのみを `tabs_to_delete` の対象とする。
- **今回のコミット差分に含まれていない既存のタブには一切干渉しない**。

### ② 全量強制同期時（`--force` / Initial Sync）

- Google Docs 上に存在する全タブ名と、GitHub 上の全有効 Markdown ファイル一覧を突合する。
- GitHub 側に存在しないタブを孤立タブとして検出し、安全に一括削除リクエストを発行する。

### ③ 初期デフォルトタブ（「タブ 1」等）のハンドリング

- Google ドキュメント新規作成時に自動生成される初期タブ（`Tab 1` / `タブ 1` / `名称未設定タブ`）は、**最初の有効なドキュメントタブが 1 つ以上作成された後にのみ安全に削除** する。
- 同期対象タブが存在しない状態で初期タブを先行削除することは物理的に禁止する。

### ④ 孤立タブ検出ロジック (`detect_orphan_tabs`)

```python
def detect_orphan_tabs(
    existing_tabs: Dict[str, str],       # {tab_title: tab_id}
    valid_github_titles: List[str],      # ['concepts_about_md', ...]
    default_tab_names: Optional[List[str]] = None
) -> List[str]:
    """
    Google Docs 側に残存している孤立タブの tab_id リストを安全に抽出する。
    全量同期（--force）時のみ実行される。
    """
    if default_tab_names is None:
        default_tab_names = ["タブ 1", "Tab 1", "名称未設定タブ", "Untitled tab"]

    valid_set = set(valid_github_titles)
    orphan_tab_ids = []

    for title, tab_id in existing_tabs.items():
        # 有効なGitHubファイル一覧になく、かつ初期デフォルトタブでもないものを孤立タブと判定
        if title not in valid_set and title not in default_tab_names:
            orphan_tab_ids.append(tab_id)

    # 全削除防止ガード: 既存タブがすべて孤立と判定された場合は即座に例外を送出
    if len(orphan_tab_ids) >= len(existing_tabs) and len(existing_tabs) >= 5:
        raise RuntimeError(
            f"Safety guard triggered: Attempting to delete all {len(existing_tabs)} tabs. Aborting."
        )

    return orphan_tab_ids

```

---

## 7. 自己修復・エラーハンドリング規約

1. **GitHub Actions 連携**:

- フェイルセーフ発動や同期エラー時は、スクリプトが非ゼロ（`sys.exit(1)`）で終了すること。
- ワークフロー側でステップ失敗を検知し、GitHub Repository へ緊急通知 Issue を自動起票すること。

2. **キャッシュ永続化ガード**:

- 同期が 100% 正常完了（Phase 1 および Phase 2 の全チャンク成功）した場合にのみ、`last_commit_sha_{owner}_{repo}.txt` を最新コミット SHA に上書き保存すること。
- 途中失敗時は次回実行時に前回のコミット SHA から再実行され、差分の取りこぼし（サイレントロスト）を防ぐこと。
