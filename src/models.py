"""データクラス・型定義・システム定数（docs/Detailed_Logic.md §1 準拠）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# システム定数（Guardrails）
# ---------------------------------------------------------------------------

#: Google Docs API の 1 ドキュメントあたり最大タブ数（100 タブ All-or-Nothing ガード）
MAX_TABS_PER_DOCUMENT: int = 100
#: Google Docs API のタブタイトル最大文字数
MAX_TAB_TITLE_LENGTH: int = 50
#: 50 文字超過時に残す先頭文字数（41 + "_" + 8 = 50）
TAB_TITLE_PREFIX_LENGTH: int = 41
#: 短縮時に付与する SHA-256 ハッシュの桁数
TAB_TITLE_HASH_LENGTH: int = 8
#: Phase 2 の 1 チャンクあたり最大タブ数（Socket Timeout 対策）
PHASE2_MAX_TABS_PER_CHUNK: int = 10
#: 全削除防止ガードが有効になる既存タブ数の下限
TOTAL_DELETION_GUARD_MIN_TABS: int = 5
#: Reusables の最大再帰展開階層
MAX_REUSABLE_DEPTH: int = 3
#: Google ドキュメント新規作成時に自動生成される初期タブ名
DEFAULT_TAB_NAMES: tuple[str, ...] = ("タブ 1", "Tab 1", "名称未設定タブ", "Untitled tab")


class SyncAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NOOP = "NOOP"


@dataclass
class FileChange:
    """GitHub 差分または全量探索から抽出された単一ファイル変更情報"""

    path: str  # 例: "content/copilot/concepts/agent-concepts.md"
    status: str  # "added", "modified", "removed", "renamed"
    raw_content: str | None = None
    resolved_content: str | None = None
    previous_path: str | None = None  # status == 'renamed' の場合のみ設定


@dataclass
class DocumentTabState:
    """Google Docs 側の既存タブ状態"""

    tab_id: str  # 例: "t.1234567890"
    title: str  # 例: "concepts_agent-concepts_md"
    end_index: int  # タブ本文の終了インデックス（文字数 + 1）


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
    watch_folders: list[WatchFolderConfig]

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repository.split("/", 1)[1]


@dataclass
class DocumentGroup:
    """同一 google_doc_id に集約された監視フォルダ群（例: Doc 1 = 4 フォルダ）"""

    google_doc_id: str
    doc_name: str
    watch_folders: list[WatchFolderConfig] = field(default_factory=list)

    @property
    def source_paths(self) -> list[str]:
        return [wf.source_path for wf in self.watch_folders]


@dataclass
class Phase1Plan:
    """Phase 1: 構造同期実行計画"""

    google_doc_id: str
    tabs_to_create: list[str]  # 作成するタブ名（フラット化済み）
    tabs_to_delete: list[str]  # 削除するタブID
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
    updates: list[Phase2TabUpdate]


@dataclass
class CompareResult:
    """GitHub Compare API の解析結果"""

    changes: list[FileChange]
    #: Compare API のファイル数上限（300）に到達し、差分取りこぼしの可能性がある
    truncated: bool = False
    #: data/reusables/ または data/variables/ に変更があった（解決後本文が変わり得る）
    data_changed: bool = False


@dataclass
class DocumentSyncReport:
    """1 ドキュメント分の同期結果サマリー"""

    google_doc_id: str
    tabs_created: list[str] = field(default_factory=list)
    tabs_deleted: list[str] = field(default_factory=list)  # 削除したタブ ID
    tabs_updated: list[str] = field(default_factory=list)  # 本文を更新したタブ名
    current_tab_count: int = 0
    predicted_tab_count: int = 0
    phase2_chunks_sent: int = 0
    dry_run: bool = False

