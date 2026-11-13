"""Google Docs API クライアント（Two-Step Pipeline: Phase 1 構造同期 / Phase 2 コンテンツ同期）。

安全ガードとリクエスト組み立ては API に依存しない純粋関数として実装し、
`GoogleDocsClient` はそれらの結果を送信するだけの薄いラッパーとする。

注意: `documents.get(includeTabsContent=False)` では `Document.tabs` が返却されないため
（第 1 タブの内容が旧来の `body` に入るのみ）、タブ一覧の取得は常に `includeTabsContent=True` で行う。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from src.models import (
    DEFAULT_TAB_NAMES,
    MAX_TABS_PER_DOCUMENT,
    PHASE2_MAX_TABS_PER_CHUNK,
    TOTAL_DELETION_GUARD_MIN_TABS,
    DocumentSyncReport,
    DocumentTabState,
    Phase1Plan,
    Phase2Plan,
    Phase2TabUpdate,
)

logger = logging.getLogger(__name__)

DOCS_SCOPES = ["https://www.googleapis.com/auth/documents"]


class SafetyGuardError(RuntimeError):
    """フェイルセーフ（100 タブ / 全削除防止 / 整合性）発動による Abort。"""


# ---------------------------------------------------------------------------
# ドキュメント構造の解析
# ---------------------------------------------------------------------------


def _tab_end_index(tab: dict[str, Any]) -> int:
    content = tab.get("documentTab", {}).get("body", {}).get("content") or []
    if not content:
        return 1
    return int(content[-1].get("endIndex", 1))


def extract_tabs(document: dict[str, Any]) -> list[DocumentTabState]:
    """documents.get(includeTabsContent=True) の結果からタブ一覧を抽出する。

    子タブは作成禁止だが、手動作成された場合もタブ数上限の計算と孤立判定に含めるため、
    警告を出した上でフラットに列挙する。
    """
    result: list[DocumentTabState] = []

    def _walk(tabs: list[dict[str, Any]], level: int) -> None:
        for tab in tabs:
            props = tab.get("tabProperties", {})
            tab_id = props.get("tabId")
            if not tab_id:
                continue
            if level > 0:
                logger.warning("Nested child tab detected (%s: %r); nested tabs are not supported.", tab_id, props.get("title"))
            result.append(DocumentTabState(tab_id=tab_id, title=props.get("title", ""), end_index=_tab_end_index(tab)))
            _walk(tab.get("childTabs") or [], level + 1)

    _walk(document.get("tabs") or [], 0)
    return result


# ---------------------------------------------------------------------------
# Phase 1: 計画とガード
# ---------------------------------------------------------------------------


def detect_orphan_tabs(
    existing_tabs: dict[str, str],
    valid_github_titles: list[str],
    default_tab_names: Iterable[str] | None = None,
) -> list[str]:
    """
    Google Docs 側に残存している孤立タブの tab_id リストを安全に抽出する。
    全量同期（--force）時のみ実行される。
    """
    defaults = set(DEFAULT_TAB_NAMES if default_tab_names is None else default_tab_names)
    valid_set = set(valid_github_titles)
    orphan_tab_ids = [tab_id for title, tab_id in existing_tabs.items() if title not in valid_set and title not in defaults]

    # 全削除防止ガード: 既存タブがすべて孤立と判定された場合は即座に例外を送出
    if len(orphan_tab_ids) >= len(existing_tabs) and len(existing_tabs) >= TOTAL_DELETION_GUARD_MIN_TABS:
        raise SafetyGuardError(f"Safety guard triggered: Attempting to delete all {len(existing_tabs)} tabs. Aborting.")
    return orphan_tab_ids


def build_phase1_plan(
    google_doc_id: str,
    current_tabs: list[DocumentTabState],
    desired_titles: list[str],
    delete_titles: list[str] | None = None,
    force: bool = False,
) -> Phase1Plan:
    """
    Phase 1 実行計画を生成し、すべての安全ガードを評価する（API 呼び出しなし）。

    Args:
        current_tabs: ドキュメントの現在のタブ一覧
        desired_titles: 今回 本文を同期するタブ名（全量同期時は GitHub 上の全有効ファイル）
        delete_titles: 差分同期時に削除すべきタブ名（removed / renamed 旧パス）
        force: 全量同期モード（孤立タブを自動検出して削除する）

    Raises:
        SafetyGuardError: 全削除防止ガード・100 タブガード・0 タブ判定のいずれかが発動した場合
    """
    desired = list(dict.fromkeys(desired_titles))
    desired_set = set(desired)
    defaults = set(DEFAULT_TAB_NAMES)

    by_title: dict[str, DocumentTabState] = {}
    duplicate_ids: list[str] = []
    for tab in current_tabs:
        if tab.title in by_title:
            logger.warning("Duplicated tab title %r in document %s (tab %s).", tab.title, google_doc_id, tab.tab_id)
            duplicate_ids.append(tab.tab_id)
        else:
            by_title[tab.title] = tab

    tabs_to_create = [t for t in desired if t not in by_title]

    delete_ids: list[str] = []
    if force:
        delete_ids.extend(detect_orphan_tabs({t: s.tab_id for t, s in by_title.items()}, desired))
        # 同名重複タブは 2 件目以降を整理（同期先が一意に定まらないため）
        delete_ids.extend(duplicate_ids)
    else:
        # 差分同期: 今回の差分で明示的に削除となったタブのみ（それ以外には一切干渉しない）
        for title in dict.fromkeys(delete_titles or []):
            if title in by_title and title not in desired_set:
                delete_ids.append(by_title[title].tab_id)

    # 初期デフォルトタブ: 有効なタブが 1 つ以上残る（または作成される）場合に限り削除
    deleting = set(delete_ids)
    remaining_valid = sum(
        1 for t in current_tabs if t.title not in defaults and t.tab_id not in deleting
    ) + len(tabs_to_create)
    if remaining_valid > 0:
        for tab in current_tabs:
            if tab.title in defaults and tab.tab_id not in deleting:
                delete_ids.append(tab.tab_id)
                deleting.add(tab.tab_id)

    current_count = len(current_tabs)
    predicted = current_count - len(delete_ids) + len(tabs_to_create)

    # 全削除防止ガード（既存 5 件以上かつ全タブが削除対象）
    if current_count >= TOTAL_DELETION_GUARD_MIN_TABS and len(delete_ids) >= current_count:
        raise SafetyGuardError(
            f"Safety guard triggered: Attempting to delete all {current_count} tabs of {google_doc_id}. Aborting."
        )
    # 0 タブ判定（Google Docs は最低 1 タブを強制する）
    if predicted <= 0:
        raise SafetyGuardError(f"Safety guard triggered: document {google_doc_id} would have {predicted} tabs. Aborting.")
    # 100 タブ All-or-Nothing ガード
    if predicted > MAX_TABS_PER_DOCUMENT:
        raise SafetyGuardError(
            f"100-tab guard triggered for {google_doc_id}: predicted {predicted} tabs "
            f"(current={current_count}, delete={len(delete_ids)}, create={len(tabs_to_create)}) "
            f"exceeds {MAX_TABS_PER_DOCUMENT}. Aborting without any API request."
        )

    return Phase1Plan(
        google_doc_id=google_doc_id,
        tabs_to_create=tabs_to_create,
        tabs_to_delete=delete_ids,
        predicted_tab_count=predicted,
    )


def build_phase1_requests(plan: Phase1Plan) -> list[dict[str, Any]]:
    """addDocumentTab を配列前半、deleteTab を後半に配置したリクエスト配列を生成する。"""
    requests: list[dict[str, Any]] = [{"addDocumentTab": {"tabProperties": {"title": t}}} for t in plan.tabs_to_create]
    requests.extend({"deleteTab": {"tabId": tab_id}} for tab_id in plan.tabs_to_delete)
    validate_phase1_order(requests)
    return requests


def validate_phase1_order(requests: list[dict[str, Any]]) -> None:
    """作成先行順序（全 addDocumentTab が全 deleteTab より前）を検証する。"""
    seen_delete = False
    for req in requests:
        if "deleteTab" in req:
            seen_delete = True
        elif "addDocumentTab" in req and seen_delete:
            raise SafetyGuardError("Phase 1 ordering violation: addDocumentTab must precede every deleteTab.")


# ---------------------------------------------------------------------------
# Phase 2: コンテンツ同期
# ---------------------------------------------------------------------------


def build_phase2_plan(
    google_doc_id: str, tabs: list[DocumentTabState], contents: dict[str, str]
) -> Phase2Plan:
    """Re-fetch 後のタブ一覧と {タブ名: 解決済み本文} から Phase 2 計画を生成する。"""
    by_title: dict[str, DocumentTabState] = {}
    for tab in tabs:
        by_title.setdefault(tab.title, tab)
    missing = [t for t in contents if t not in by_title]
    if missing:
        raise SafetyGuardError(f"Tabs not found after Phase 1 in {google_doc_id}: {missing[:10]}")
    updates = [
        Phase2TabUpdate(
            tab_id=by_title[title].tab_id,
            tab_title=title,
            target_end_index=by_title[title].end_index,
            content_to_insert=content,
        )
        for title, content in contents.items()
    ]
    return Phase2Plan(google_doc_id=google_doc_id, updates=updates)


def build_tab_update_requests(update: Phase2TabUpdate) -> list[dict[str, Any]]:
    """単一タブの Clear（deleteContentRange）+ Insert（insertText）ペアを生成する。"""
    requests: list[dict[str, Any]] = []
    if update.target_end_index > 2:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "tabId": update.tab_id,
                        "segmentId": "",
                        "startIndex": 1,
                        "endIndex": update.target_end_index - 1,
                    }
                }
            }
        )
    if update.content_to_insert:
        requests.append(
            {
                "insertText": {
                    "location": {"tabId": update.tab_id, "segmentId": "", "index": 1},
                    "text": update.content_to_insert,
                }
            }
        )
    return requests


def build_phase2_chunks(
    plan: Phase2Plan, max_tabs_per_chunk: int = PHASE2_MAX_TABS_PER_CHUNK
) -> list[list[dict[str, Any]]]:
    """最大 10 タブ（20 リクエスト）単位でチャンク分割する。タブ単位のペアは分断しない。"""
    if max_tabs_per_chunk < 1:
        raise ValueError("max_tabs_per_chunk must be >= 1")
    chunks: list[list[dict[str, Any]]] = []
    for start in range(0, len(plan.updates), max_tabs_per_chunk):
        chunk: list[dict[str, Any]] = []
        for update in plan.updates[start : start + max_tabs_per_chunk]:
            chunk.extend(build_tab_update_requests(update))
        if chunk:
            chunks.append(chunk)
    return chunks


def summarize_requests(requests: list[dict[str, Any]]) -> str:
    """ログ出力用にリクエストを要約する（本文テキストは長さのみ）。"""
    summary: list[dict[str, Any]] = []
    for req in requests:
        kind = next(iter(req))
        body = dict(req[kind])
        if kind == "insertText":
            body = {"location": body.get("location"), "text_length": len(body.get("text", ""))}
        summary.append({kind: body})
    return json.dumps(summary, ensure_ascii=False)[:4000]


# ---------------------------------------------------------------------------
# API ラッパー
# ---------------------------------------------------------------------------


class GoogleDocsClient:
    """googleapiclient の Docs service をラップする。service は注入可能（テストではモック）。"""

    def __init__(self, service: Any) -> None:
        self.service = service

    @classmethod
    def from_service_account(cls, credentials_path: str) -> GoogleDocsClient:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(credentials_path, scopes=DOCS_SCOPES)
        return cls(build("docs", "v1", credentials=credentials, cache_discovery=False))

    # ------------------------------------------------------------------ calls

    def get_tabs(self, google_doc_id: str) -> list[DocumentTabState]:
        try:
            document = self.service.documents().get(documentId=google_doc_id, includeTabsContent=True).execute()
        except Exception as exc:
            _log_http_error("documents.get", google_doc_id, exc, None)
            raise
        return extract_tabs(document)

    def batch_update(self, google_doc_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return (
                self.service.documents()
                .batchUpdate(documentId=google_doc_id, body={"requests": requests})
                .execute()
            )
        except Exception as exc:
            _log_http_error("documents.batchUpdate", google_doc_id, exc, requests)
            raise

    # ------------------------------------------------------------- pipeline

    def execute_phase1(self, plan: Phase1Plan, current_tabs: list[DocumentTabState]) -> dict[str, Any] | None:
        """Phase 1 を送信する。削除対象 ID が直前取得のタブ一覧に存在するかを事前検証する。"""
        known_ids = {t.tab_id for t in current_tabs}
        unknown = [i for i in plan.tabs_to_delete if i not in known_ids]
        if unknown:
            raise SafetyGuardError(f"deleteTab targets not found in current document state: {unknown}")
        requests = build_phase1_requests(plan)
        if not requests:
            logger.info("Phase 1: no structural changes for %s.", plan.google_doc_id)
            return None
        logger.info(
            "Phase 1: %s create=%d delete=%d predicted=%d",
            plan.google_doc_id, len(plan.tabs_to_create), len(plan.tabs_to_delete), plan.predicted_tab_count,
        )
        return self.batch_update(plan.google_doc_id, requests)

    def execute_phase2(self, plan: Phase2Plan) -> int:
        """Phase 2 をチャンク単位で順次送信し、送信したチャンク数を返す。"""
        chunks = build_phase2_chunks(plan)
        for idx, chunk in enumerate(chunks, start=1):
            logger.info("Phase 2: %s chunk %d/%d (%d requests)", plan.google_doc_id, idx, len(chunks), len(chunk))
            self.batch_update(plan.google_doc_id, chunk)
        return len(chunks)

    def sync_document(
        self,
        google_doc_id: str,
        contents: dict[str, str],
        delete_titles: list[str] | None = None,
        force: bool = False,
        dry_run: bool = False,
        all_valid_titles: list[str] | None = None,
    ) -> DocumentSyncReport:
        """
        1 ドキュメント分の Two-Step 同期を実行する。

        Args:
            contents: {タブ名: 解決済み本文}（今回本文を更新するタブ）
            delete_titles: 差分同期時の削除対象タブ名
            force: 全量同期（孤立タブ削除あり）
            dry_run: 読み取り（documents.get）と計画・ガード評価のみ行い、batchUpdate を送信しない
            all_valid_titles: 全量同期時の GitHub 側全有効タブ名（省略時は contents のキー）
        """
        current_tabs = self.get_tabs(google_doc_id)
        desired = list(all_valid_titles) if (force and all_valid_titles is not None) else list(contents)
        plan = build_phase1_plan(google_doc_id, current_tabs, desired, delete_titles, force=force)

        report = DocumentSyncReport(
            google_doc_id=google_doc_id,
            tabs_created=list(plan.tabs_to_create),
            tabs_deleted=list(plan.tabs_to_delete),
            tabs_updated=list(contents),
            current_tab_count=len(current_tabs),
            predicted_tab_count=plan.predicted_tab_count,
            dry_run=dry_run,
        )
        if dry_run:
            logger.info(
                "[dry-run] %s current=%d create=%d delete=%d update=%d predicted=%d",
                google_doc_id, len(current_tabs), len(plan.tabs_to_create),
                len(plan.tabs_to_delete), len(contents), plan.predicted_tab_count,
            )
            return report

        self.execute_phase1(plan, current_tabs)
        refreshed = self.get_tabs(google_doc_id)  # Re-fetch: 新規 tabId と endIndex を取得
        report.phase2_chunks_sent = self.execute_phase2(build_phase2_plan(google_doc_id, refreshed, contents))
        return report


def _log_http_error(operation: str, google_doc_id: str, exc: Exception, requests: list[dict[str, Any]] | None) -> None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    logger.error(
        "Google Docs API error: op=%s doc=%s status=%s body=%s payload=%s",
        operation, google_doc_id, status, str(content)[:2000],
        summarize_requests(requests) if requests else None,
    )
