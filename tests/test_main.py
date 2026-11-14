"""main.py の単体・結合テスト（GitHub / Google Docs 通信はすべてモック）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config import ConfigError
from src.docs_client import GoogleDocsClient, SafetyGuardError
from src.github_client import GitHubClient
from src.main import commit_sha_cache_path, main, read_cached_sha, scope_changes_to_group, sync_target, write_cached_sha
from src.models import CompareResult, DocumentTabState, FileChange, SyncTargetConfig, WatchFolderConfig

DOC1_ID = "DOC1"
DOC2_ID = "DOC2"


def _target(multi_doc: bool = False) -> SyncTargetConfig:
    folders = [
        WatchFolderConfig("content/copilot/concepts", DOC1_ID, "Doc 1"),
        WatchFolderConfig("content/copilot/get-started", DOC1_ID, "Doc 1"),
    ]
    if multi_doc:
        folders.append(WatchFolderConfig("content/copilot/reference", DOC2_ID, "Doc 2"))
    return SyncTargetConfig(repository="github/docs", watch_folders=folders)


def _doc_json(doc_id: str, tab_titles: list[str], end_index: int = 10) -> dict[str, Any]:
    return {
        "documentId": doc_id,
        "tabs": [
            {
                "tabProperties": {"tabId": f"t.{i}", "title": title},
                "documentTab": {"body": {"content": [{"endIndex": 1}, {"endIndex": end_index}]}},
            }
            for i, title in enumerate(tab_titles)
        ],
    }


def _mock_gh(
    head_sha: str = "head1234567890",
    folder_files: dict[str, list[str]] | None = None,
    raw_files: dict[str, str] | None = None,
    variables: dict[str, dict[str, str]] | None = None,
    compare_result: CompareResult | None = None,
) -> MagicMock:
    gh = MagicMock(spec=GitHubClient)
    gh.get_head_sha.return_value = head_sha
    files_map = folder_files or {
        "content/copilot/concepts": ["content/copilot/concepts/about.md"],
        "content/copilot/get-started": ["content/copilot/get-started/quickstart.md"],
        "content/copilot/reference": ["content/copilot/reference/cli.md"],
    }
    gh.list_folder_files.side_effect = lambda folder, ref: list(files_map.get(folder, []))

    raw_map = raw_files or {
        "content/copilot/concepts/about.md": (
            "---\ntitle: 'About {% data variables.product.prodname_copilot %}'\n"
            "intro: 'Intro text.'\n---\n\nBody {% octicon \"check\" %}\n"
        ),
        "content/copilot/get-started/quickstart.md": "---\ntitle: Quickstart\n---\n\nStart here.\n",
        "content/copilot/reference/cli.md": "---\ntitle: CLI\n---\n\nReference.\n",
    }

    def _fetch_contents(changes: list[FileChange], ref: str) -> list[FileChange]:
        for c in changes:
            if c.status != "removed":
                c.raw_content = raw_map.get(c.path, f"# {c.path}\n")
        return changes

    gh.fetch_contents.side_effect = _fetch_contents
    gh.load_variables.return_value = variables or {"product": {"prodname_copilot": "GitHub Copilot"}}
    gh.make_reusable_loader.return_value = lambda rel_path: raw_map.get(f"data/reusables/{rel_path}.md")
    if compare_result is not None:
        gh.compare.return_value = compare_result
    return gh


def _mock_docs(before_titles: list[str], after_titles: list[str] | None = None) -> tuple[GoogleDocsClient, MagicMock]:
    service = MagicMock()
    after = after_titles if after_titles is not None else before_titles
    service.documents.return_value.get.return_value.execute.side_effect = [
        _doc_json(DOC1_ID, before_titles),
        _doc_json(DOC1_ID, after, end_index=1),
    ]
    return GoogleDocsClient(service), service


class TestShaCacheHelpers:
    def test_read_write_sha(self, tmp_path: Path) -> None:
        target = _target()
        path = commit_sha_cache_path(target, tmp_path)
        assert path.name == "last_commit_sha_github_docs.txt"
        assert read_cached_sha(path) is None
        write_cached_sha(path, "abc123")
        assert read_cached_sha(path) == "abc123"


class TestScopeChangesToGroup:
    def test_cross_group_rename_split(self) -> None:
        changes = [
            FileChange(
                path="content/copilot/reference/moved.md",
                status="renamed",
                previous_path="content/copilot/concepts/moved.md",
            )
        ]
        doc1_scoped = scope_changes_to_group(changes, ["content/copilot/concepts", "content/copilot/get-started"])
        doc2_scoped = scope_changes_to_group(changes, ["content/copilot/reference"])
        assert [(c.path, c.status) for c in doc1_scoped] == [("content/copilot/concepts/moved.md", "removed")]
        assert [(c.path, c.status) for c in doc2_scoped] == [("content/copilot/reference/moved.md", "added")]

    def test_nested_cross_group_scope_assigns_to_deepest_folder(self) -> None:
        doc6_folders = ["content/copilot/tutorials/copilot-cookbook"]
        doc7_folders = ["content/copilot/tutorials", "content/copilot/tutorials/budgets"]
        all_folders = doc6_folders + doc7_folders
        changes = [
            FileChange(path="content/copilot/tutorials/copilot-cookbook/recipe.md", status="modified"),
            FileChange(path="content/copilot/tutorials/vibe-coding.md", status="modified"),
            FileChange(path="content/copilot/tutorials/budgets/limit.md", status="added"),
        ]
        doc6_scoped = scope_changes_to_group(changes, doc6_folders, all_folders)
        doc7_scoped = scope_changes_to_group(changes, doc7_folders, all_folders)
        assert [c.path for c in doc6_scoped] == ["content/copilot/tutorials/copilot-cookbook/recipe.md"]
        assert [c.path for c in doc7_scoped] == [
            "content/copilot/tutorials/vibe-coding.md",
            "content/copilot/tutorials/budgets/limit.md",
        ]


class TestFullAndInitialSync:
    def test_nested_watch_folders_across_groups_do_not_duplicate_in_full_sync(self, tmp_path: Path) -> None:
        target = SyncTargetConfig(
            repository="github/docs",
            watch_folders=[
                WatchFolderConfig("content/copilot/tutorials/copilot-cookbook", "DOC6", "Doc 6"),
                WatchFolderConfig("content/copilot/tutorials", DOC1_ID, "Doc 7"),
                WatchFolderConfig("content/copilot/tutorials/budgets", DOC1_ID, "Doc 7"),
            ],
        )
        all_tutorial_files = [
            "content/copilot/tutorials/vibe-coding.md",
            "content/copilot/tutorials/budgets/limit.md",
            "content/copilot/tutorials/copilot-cookbook/recipe.md",
        ]
        gh = _mock_gh(
            head_sha="sha_doc7",
            folder_files={
                "content/copilot/tutorials": all_tutorial_files,
                "content/copilot/tutorials/budgets": ["content/copilot/tutorials/budgets/limit.md"],
                "content/copilot/tutorials/copilot-cookbook": ["content/copilot/tutorials/copilot-cookbook/recipe.md"],
            },
        )
        docs, _ = _mock_docs(
            before_titles=["タブ 1"],
            after_titles=["tutorials_vibe-coding_md", "budgets_limit_md"],
        )
        reports = sync_target(target, gh, docs, force=True, dry_run=True, doc_id_filter=DOC1_ID, cache_dir=tmp_path, output_dir=tmp_path / "out")
        assert len(reports) == 1
        assert reports[0].predicted_tab_count == 2
        assert reports[0].tabs_created == ["tutorials_vibe-coding_md", "budgets_limit_md"]
    def test_initial_sync_without_cache_runs_full_and_writes_sha(self, tmp_path: Path) -> None:
        gh = _mock_gh(head_sha="sha_new_123456")
        docs, service = _mock_docs(
            before_titles=["タブ 1"],
            after_titles=["concepts_about_md", "get-started_quickstart_md"],
        )
        reports = sync_target(_target(), gh, docs, cache_dir=tmp_path)
        assert len(reports) == 1
        assert reports[0].predicted_tab_count == 2
        assert read_cached_sha(commit_sha_cache_path(_target(), tmp_path)) == "sha_new_123456"
        assert service.documents.return_value.batchUpdate.call_count == 2  # Phase 1 + Phase 2

    def test_up_to_date_sha_is_noop(self, tmp_path: Path) -> None:
        target = _target()
        write_cached_sha(commit_sha_cache_path(target, tmp_path), "same_sha")
        gh = _mock_gh(head_sha="same_sha")
        docs, service = _mock_docs(["concepts_about_md"])
        reports = sync_target(target, gh, docs, cache_dir=tmp_path)
        assert reports == []
        gh.compare.assert_not_called()
        service.documents.return_value.get.assert_not_called()


class TestIncrementalSync:
    def test_incremental_sync_updates_and_deletes(self, tmp_path: Path) -> None:
        target = _target()
        write_cached_sha(commit_sha_cache_path(target, tmp_path), "old_sha")
        compare = CompareResult(
            changes=[
                FileChange("content/copilot/concepts/about.md", "modified"),
                FileChange(
                    "content/copilot/get-started/quickstart.md",
                    "renamed",
                    previous_path="content/copilot/get-started/old-start.md",
                ),
            ]
        )
        gh = _mock_gh(head_sha="new_sha", compare_result=compare)
        docs, service = _mock_docs(
            before_titles=["concepts_about_md", "get-started_old-start_md", "concepts_keep_md"],
            after_titles=["concepts_about_md", "get-started_quickstart_md", "concepts_keep_md"],
        )
        reports = sync_target(target, gh, docs, cache_dir=tmp_path)
        assert len(reports) == 1
        assert reports[0].tabs_created == ["get-started_quickstart_md"]
        assert reports[0].tabs_deleted == ["t.1"]  # old-start のみ削除、keep には干渉しない
        assert sorted(reports[0].tabs_updated) == ["concepts_about_md", "get-started_quickstart_md"]
        assert read_cached_sha(commit_sha_cache_path(target, tmp_path)) == "new_sha"
        assert service.documents.return_value.batchUpdate.call_count == 2

    @pytest.mark.parametrize(
        "compare",
        [
            CompareResult(changes=[], truncated=True),
            CompareResult(changes=[], data_changed=True),
        ],
    )
    def test_fallback_to_full_sync_on_truncated_or_data_change(
        self, tmp_path: Path, compare: CompareResult
    ) -> None:
        target = _target()
        write_cached_sha(commit_sha_cache_path(target, tmp_path), "old_sha")
        gh = _mock_gh(head_sha="new_sha", compare_result=compare)
        docs, _ = _mock_docs(
            before_titles=["concepts_about_md", "get-started_quickstart_md", "concepts_orphan_md"],
            after_titles=["concepts_about_md", "get-started_quickstart_md"],
        )
        reports = sync_target(target, gh, docs, cache_dir=tmp_path)
        assert reports[0].tabs_deleted == ["t.2"]  # 全量同期フォールバックにより orphan も削除
        gh.list_folder_files.assert_called()


class TestGuardAndFailurePreservesSha:
    def test_100_tab_guard_abort_does_not_update_sha_cache(self, tmp_path: Path) -> None:
        target = _target()
        sha_path = commit_sha_cache_path(target, tmp_path)
        write_cached_sha(sha_path, "previous_good_sha")

        too_many = [f"content/copilot/concepts/f{i}.md" for i in range(101)]
        gh = _mock_gh(
            head_sha="bad_sha",
            folder_files={"content/copilot/concepts": too_many, "content/copilot/get-started": []},
        )
        docs, service = _mock_docs(before_titles=["タブ 1"])

        with pytest.raises(SafetyGuardError, match="100-tab guard"):
            sync_target(target, gh, docs, force=True, cache_dir=tmp_path)

        service.documents.return_value.batchUpdate.assert_not_called()
        assert read_cached_sha(sha_path) == "previous_good_sha"

    def test_phase2_chunk_failure_does_not_update_sha_cache(self, tmp_path: Path) -> None:
        target = _target()
        sha_path = commit_sha_cache_path(target, tmp_path)
        write_cached_sha(sha_path, "previous_good_sha")

        gh = _mock_gh(head_sha="failing_sha")
        docs, service = _mock_docs(
            before_titles=["タブ 1"],
            after_titles=["concepts_about_md", "get-started_quickstart_md"],
        )
        service.documents.return_value.batchUpdate.return_value.execute.side_effect = [
            {},  # Phase 1 succeeds
            RuntimeError("socket timeout during Phase 2"),
        ]

        with pytest.raises(RuntimeError, match="socket timeout"):
            sync_target(target, gh, docs, force=True, cache_dir=tmp_path)

        assert read_cached_sha(sha_path) == "previous_good_sha"


class TestDryRunAndFilters:
    def test_dry_run_writes_files_and_never_calls_batch_update_or_writes_sha(self, tmp_path: Path) -> None:
        target = _target()
        sha_path = commit_sha_cache_path(target, tmp_path)
        out_dir = tmp_path / "dry_run"
        gh = _mock_gh(head_sha="dry_sha")
        docs, service = _mock_docs(before_titles=["タブ 1"])

        reports = sync_target(target, gh, docs, force=True, dry_run=True, cache_dir=tmp_path, output_dir=out_dir)

        assert len(reports) == 1
        assert reports[0].dry_run is True
        assert reports[0].predicted_tab_count == 2
        service.documents.return_value.batchUpdate.assert_not_called()
        assert not sha_path.exists()

        written = sorted(p.name for p in (out_dir / DOC1_ID).glob("*.md"))
        assert written == ["concepts_about_md.md", "get-started_quickstart_md.md"]
        about_text = (out_dir / DOC1_ID / "concepts_about_md.md").read_text(encoding="utf-8")
        assert about_text.startswith("# About GitHub Copilot\n\nIntro text.\n\nBody ✓\n")
        assert "{%" not in about_text

    def test_doc_id_filter_unknown_raises(self, tmp_path: Path) -> None:
        gh = _mock_gh()
        docs, _ = _mock_docs(["タブ 1"])
        with pytest.raises(ConfigError, match="not found"):
            sync_target(_target(), gh, docs, doc_id_filter="UNKNOWN_ID", cache_dir=tmp_path)

    def test_partial_doc_id_filter_does_not_overwrite_repo_sha(self, tmp_path: Path) -> None:
        target = _target(multi_doc=True)
        sha_path = commit_sha_cache_path(target, tmp_path)
        gh = _mock_gh(head_sha="sha_partial")
        docs, _ = _mock_docs(
            before_titles=["タブ 1"],
            after_titles=["concepts_about_md", "get-started_quickstart_md"],
        )
        sync_target(target, gh, docs, force=True, doc_id_filter=DOC1_ID, cache_dir=tmp_path)
        assert not sha_path.exists()

    def test_cli_main_returns_1_on_error(self, tmp_path: Path) -> None:
        code = main(["--config", str(tmp_path / "nonexistent.yaml")])
        assert code == 1

