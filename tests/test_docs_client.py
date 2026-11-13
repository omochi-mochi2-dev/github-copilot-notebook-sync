"""docs_client.py の単体テスト（Google Docs API service を unittest.mock でモック。実通信なし）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from src.docs_client import (
    GoogleDocsClient,
    SafetyGuardError,
    build_phase1_plan,
    build_phase1_requests,
    build_phase2_chunks,
    build_phase2_plan,
    build_tab_update_requests,
    detect_orphan_tabs,
    extract_tabs,
    validate_phase1_order,
)
from src.models import DocumentTabState, Phase1Plan, Phase2Plan, Phase2TabUpdate

DOC = "doc-1"


def tabs(*titles: str, end_index: int = 10) -> list[DocumentTabState]:
    return [DocumentTabState(tab_id=f"t.{i}", title=t, end_index=end_index) for i, t in enumerate(titles)]


def titles(n: int, prefix: str = "concepts_f") -> list[str]:
    return [f"{prefix}{i}_md" for i in range(n)]


def doc_json(tab_list: list[DocumentTabState], child: dict[str, Any] | None = None) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for t in tab_list:
        entry: dict[str, Any] = {
            "tabProperties": {"tabId": t.tab_id, "title": t.title},
            "documentTab": {"body": {"content": [{"endIndex": 1}, {"endIndex": t.end_index}]}},
        }
        out.append(entry)
    if child and out:
        out[0]["childTabs"] = [child]
    return {"documentId": DOC, "tabs": out}


# ---------------------------------------------------------------------------
# 構造解析
# ---------------------------------------------------------------------------


class TestExtractTabs:
    def test_extract(self) -> None:
        result = extract_tabs(doc_json(tabs("a", "b", end_index=42)))
        assert [(t.tab_id, t.title, t.end_index) for t in result] == [("t.0", "a", 42), ("t.1", "b", 42)]

    def test_empty_body_defaults_to_1(self) -> None:
        doc = {"tabs": [{"tabProperties": {"tabId": "t.x", "title": "x"}, "documentTab": {"body": {}}}]}
        assert extract_tabs(doc)[0].end_index == 1

    def test_child_tabs_flattened_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        child = {"tabProperties": {"tabId": "t.child", "title": "child"}, "documentTab": {"body": {"content": []}}}
        result = extract_tabs(doc_json(tabs("parent"), child))
        assert [t.tab_id for t in result] == ["t.0", "t.child"]
        assert "Nested child tab" in caplog.text


# ---------------------------------------------------------------------------
# 100 タブ All-or-Nothing ガード
# ---------------------------------------------------------------------------


class TestHundredTabGuard:
    def test_exactly_100_passes(self) -> None:
        plan = build_phase1_plan(DOC, tabs(*titles(90)), titles(100), force=True)
        assert plan.predicted_tab_count == 100
        assert len(plan.tabs_to_create) == 10

    def test_101_aborts(self) -> None:
        with pytest.raises(SafetyGuardError, match="100-tab guard"):
            build_phase1_plan(DOC, tabs(*titles(90)), titles(101), force=True)

    def test_daily_mode_counts_existing_untouched_tabs(self) -> None:
        with pytest.raises(SafetyGuardError, match="predicted 101"):
            build_phase1_plan(DOC, tabs(*titles(95)), titles(6, prefix="new_"), force=False)

    def test_deletions_offset_creations(self) -> None:
        current = tabs(*titles(100))
        plan = build_phase1_plan(
            DOC, current, ["brand_new_md"], delete_titles=["concepts_f0_md"], force=False
        )
        assert plan.predicted_tab_count == 100

    def test_guard_sends_zero_api_requests(self) -> None:
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = doc_json(tabs(*titles(95)))
        client = GoogleDocsClient(service)
        contents = {t: "body\n" for t in titles(101)}
        with pytest.raises(SafetyGuardError):
            client.sync_document(DOC, contents, force=True)
        service.documents.return_value.batchUpdate.assert_not_called()

    def test_guard_also_applies_in_dry_run(self) -> None:
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.return_value = doc_json(tabs("Tab 1"))
        with pytest.raises(SafetyGuardError):
            GoogleDocsClient(service).sync_document(DOC, {t: "x" for t in titles(101)}, force=True, dry_run=True)
        service.documents.return_value.batchUpdate.assert_not_called()


# ---------------------------------------------------------------------------
# 全削除防止ガード
# ---------------------------------------------------------------------------


class TestTotalDeletionGuard:
    def test_detect_orphans_spec(self) -> None:
        existing = {"a": "t.a", "b": "t.b", "タブ 1": "t.d"}
        assert detect_orphan_tabs(existing, ["a"]) == ["t.b"]

    def test_detect_orphans_all_deleted_aborts(self) -> None:
        existing = {f"x{i}": f"t.{i}" for i in range(5)}
        with pytest.raises(SafetyGuardError, match="delete all 5"):
            detect_orphan_tabs(existing, ["unrelated"])

    def test_detect_orphans_below_threshold_allowed(self) -> None:
        existing = {f"x{i}": f"t.{i}" for i in range(4)}
        assert len(detect_orphan_tabs(existing, ["unrelated"])) == 4

    def test_force_all_orphans_aborts_even_with_creations(self) -> None:
        with pytest.raises(SafetyGuardError, match="delete all"):
            build_phase1_plan(DOC, tabs(*titles(5, prefix="old_")), titles(10), force=True)

    def test_daily_mode_deleting_all_aborts(self) -> None:
        current = tabs(*titles(6))
        with pytest.raises(SafetyGuardError, match="delete all 6"):
            build_phase1_plan(DOC, current, [], delete_titles=titles(6), force=False)

    def test_zero_tab_result_aborts(self) -> None:
        with pytest.raises(SafetyGuardError, match="0 tabs"):
            build_phase1_plan(DOC, tabs("a_md", "b_md"), [], delete_titles=["a_md", "b_md"], force=False)

    def test_daily_mode_does_not_touch_unrelated_tabs(self) -> None:
        current = tabs("a_md", "b_md", "c_md")
        plan = build_phase1_plan(DOC, current, ["a_md"], delete_titles=["c_md", "not_existing_md"], force=False)
        assert plan.tabs_to_delete == ["t.2"]
        assert plan.tabs_to_create == []

    def test_force_deletes_duplicate_titles(self) -> None:
        current = tabs("a_md", "a_md", "b_md")
        plan = build_phase1_plan(DOC, current, ["a_md", "b_md"], force=True)
        assert plan.tabs_to_delete == ["t.1"]


# ---------------------------------------------------------------------------
# 初期デフォルトタブ
# ---------------------------------------------------------------------------


class TestDefaultTab:
    def test_initial_sync_replaces_default_tab(self) -> None:
        plan = build_phase1_plan(DOC, tabs("タブ 1"), titles(3), force=True)
        assert plan.tabs_to_create == titles(3)
        assert plan.tabs_to_delete == ["t.0"]
        assert plan.predicted_tab_count == 3

    def test_default_tab_kept_when_nothing_created(self) -> None:
        plan = build_phase1_plan(DOC, tabs("Tab 1"), [], force=True)
        assert plan.tabs_to_delete == []
        assert plan.predicted_tab_count == 1

    def test_default_tab_deleted_when_valid_tabs_exist(self) -> None:
        plan = build_phase1_plan(DOC, tabs("a_md", "Untitled tab"), ["a_md"], force=False)
        assert plan.tabs_to_delete == ["t.1"]


# ---------------------------------------------------------------------------
# 作成先行順序
# ---------------------------------------------------------------------------


class TestPhase1Ordering:
    def test_adds_before_deletes(self) -> None:
        plan = build_phase1_plan(DOC, tabs("タブ 1", "old1_md", "old2_md"), ["n1_md", "n2_md"], force=True)
        reqs = build_phase1_requests(plan)
        kinds = [next(iter(r)) for r in reqs]
        assert kinds == ["addDocumentTab", "addDocumentTab", "deleteTab", "deleteTab", "deleteTab"]
        assert reqs[0] == {"addDocumentTab": {"tabProperties": {"title": "n1_md"}}}
        assert {"deleteTab": {"tabId": "t.0"}} in reqs

    def test_full_replacement_below_guard_threshold(self) -> None:
        plan = build_phase1_plan(DOC, tabs(*titles(4, prefix="old_")), ["new_md"], force=True)
        kinds = [next(iter(r)) for r in build_phase1_requests(plan)]
        assert kinds[0] == "addDocumentTab"
        assert kinds.index("deleteTab") > kinds.index("addDocumentTab")

    def test_validate_order_rejects_violation(self) -> None:
        with pytest.raises(SafetyGuardError, match="ordering"):
            validate_phase1_order([{"deleteTab": {"tabId": "t.1"}}, {"addDocumentTab": {"tabProperties": {"title": "x"}}}])

    def test_execute_phase1_sends_single_ordered_batch(self) -> None:
        service = MagicMock()
        current = tabs("タブ 1")
        plan = build_phase1_plan(DOC, current, ["a_md", "b_md"], force=True)
        GoogleDocsClient(service).execute_phase1(plan, current)
        call = service.documents.return_value.batchUpdate.call_args
        sent = call.kwargs["body"]["requests"]
        assert call.kwargs["documentId"] == DOC
        assert [next(iter(r)) for r in sent] == ["addDocumentTab", "addDocumentTab", "deleteTab"]

    def test_execute_phase1_rejects_unknown_delete_target(self) -> None:
        service = MagicMock()
        plan = Phase1Plan(DOC, tabs_to_create=["a"], tabs_to_delete=["t.ghost"], predicted_tab_count=1)
        with pytest.raises(SafetyGuardError, match="not found"):
            GoogleDocsClient(service).execute_phase1(plan, tabs("x"))
        service.documents.return_value.batchUpdate.assert_not_called()

    def test_execute_phase1_noop(self) -> None:
        service = MagicMock()
        current = tabs("a_md")
        plan = build_phase1_plan(DOC, current, ["a_md"], force=True)
        assert GoogleDocsClient(service).execute_phase1(plan, current) is None
        service.documents.return_value.batchUpdate.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------


class TestPhase2:
    def test_tab_requests_clear_and_insert(self) -> None:
        reqs = build_tab_update_requests(Phase2TabUpdate("t.1", "a", 120, "Hello\n"))
        assert reqs == [
            {"deleteContentRange": {"range": {"tabId": "t.1", "segmentId": "", "startIndex": 1, "endIndex": 119}}},
            {"insertText": {"location": {"tabId": "t.1", "segmentId": "", "index": 1}, "text": "Hello\n"}},
        ]

    @pytest.mark.parametrize("end_index", [1, 2])
    def test_empty_tab_skips_clear(self, end_index: int) -> None:
        reqs = build_tab_update_requests(Phase2TabUpdate("t.1", "a", end_index, "X"))
        assert [next(iter(r)) for r in reqs] == ["insertText"]

    def test_chunking_25_tabs(self) -> None:
        updates = [Phase2TabUpdate(f"t.{i}", f"tab{i}", 50, f"body{i}") for i in range(25)]
        chunks = build_phase2_chunks(Phase2Plan(DOC, updates))
        assert [len(c) for c in chunks] == [20, 20, 10]
        assert all(len(c) <= 20 for c in chunks)
        for chunk in chunks:
            for i in range(0, len(chunk), 2):
                assert chunk[i]["deleteContentRange"]["range"]["tabId"] == chunk[i + 1]["insertText"]["location"]["tabId"]
        tab_ids = [r["insertText"]["location"]["tabId"] for c in chunks for r in c if "insertText" in r]
        assert tab_ids == [f"t.{i}" for i in range(25)]

    def test_chunking_exact_multiple(self) -> None:
        updates = [Phase2TabUpdate(f"t.{i}", f"tab{i}", 1, "x") for i in range(20)]
        assert [len(c) for c in build_phase2_chunks(Phase2Plan(DOC, updates))] == [10, 10]

    def test_chunking_empty(self) -> None:
        assert build_phase2_chunks(Phase2Plan(DOC, [])) == []

    def test_build_phase2_plan_missing_tab_aborts(self) -> None:
        with pytest.raises(SafetyGuardError, match="not found after Phase 1"):
            build_phase2_plan(DOC, tabs("a"), {"a": "x", "b": "y"})

    def test_execute_phase2_sends_chunks_sequentially(self) -> None:
        service = MagicMock()
        updates = [Phase2TabUpdate(f"t.{i}", f"tab{i}", 30, "x") for i in range(23)]
        sent = GoogleDocsClient(service).execute_phase2(Phase2Plan(DOC, updates))
        assert sent == 3
        calls = service.documents.return_value.batchUpdate.call_args_list
        assert [len(c.kwargs["body"]["requests"]) for c in calls] == [20, 20, 6]

    def test_phase2_failure_propagates_and_stops(self, caplog: pytest.LogCaptureFixture) -> None:
        service = MagicMock()
        err = Exception("socket timeout")
        err.resp = MagicMock(status=503)  # type: ignore[attr-defined]
        err.content = b'{"error": "unavailable"}'  # type: ignore[attr-defined]
        service.documents.return_value.batchUpdate.return_value.execute.side_effect = [{}, err, {}]
        updates = [Phase2TabUpdate(f"t.{i}", f"tab{i}", 30, "x" * 100) for i in range(25)]
        with pytest.raises(Exception, match="socket timeout"):
            GoogleDocsClient(service).execute_phase2(Phase2Plan(DOC, updates))
        assert service.documents.return_value.batchUpdate.call_count == 2
        assert "status=503" in caplog.text and "unavailable" in caplog.text
        assert "text_length" in caplog.text


# ---------------------------------------------------------------------------
# sync_document（Two-Step 全体）
# ---------------------------------------------------------------------------


class TestSyncDocument:
    def _service(self, before: list[DocumentTabState], after: list[DocumentTabState]) -> MagicMock:
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.side_effect = [doc_json(before), doc_json(after)]
        return service

    def test_initial_force_sync_flow(self) -> None:
        new_titles = titles(12)
        after = [DocumentTabState(f"t.n{i}", t, 1) for i, t in enumerate(new_titles)]
        service = self._service(tabs("タブ 1"), after)
        contents = {t: f"# {t}\n" for t in new_titles}

        report = GoogleDocsClient(service).sync_document(DOC, contents, force=True)

        docs = service.documents.return_value
        assert docs.get.call_count == 2
        assert all(c.kwargs["includeTabsContent"] is True for c in docs.get.call_args_list)
        batches = [c.kwargs["body"]["requests"] for c in docs.batchUpdate.call_args_list]
        assert len(batches) == 3  # Phase 1 + Phase 2 (10 + 2 タブ)
        assert [next(iter(r)) for r in batches[0]] == ["addDocumentTab"] * 12 + ["deleteTab"]
        assert len(batches[1]) == 10 and len(batches[2]) == 2  # 空タブのためクリアなし
        assert batches[1][0]["insertText"]["location"]["tabId"] == "t.n0"
        assert report.predicted_tab_count == 12
        assert report.phase2_chunks_sent == 2
        assert report.tabs_deleted == ["t.0"]

    def test_dry_run_reads_only(self) -> None:
        service = self._service(tabs("タブ 1"), [])
        report = GoogleDocsClient(service).sync_document(DOC, {t: "x" for t in titles(85)}, force=True, dry_run=True)
        docs = service.documents.return_value
        docs.batchUpdate.assert_not_called()
        assert docs.get.call_count == 1
        assert report.dry_run is True
        assert report.predicted_tab_count == 85
        assert len(report.tabs_created) == 85

    def test_force_uses_all_valid_titles_for_orphans(self) -> None:
        existing = tabs("a_md", "b_md", "orphan_md")
        service = self._service(existing, existing)
        report = GoogleDocsClient(service).sync_document(
            DOC, {"a_md": "A"}, force=True, dry_run=True, all_valid_titles=["a_md", "b_md"]
        )
        assert report.tabs_deleted == ["t.2"]
