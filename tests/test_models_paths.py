"""models.py / path_utils.py の単体テスト。"""

from __future__ import annotations

import hashlib

import pytest

from src.models import (
    DEFAULT_TAB_NAMES,
    MAX_TAB_TITLE_LENGTH,
    MAX_TABS_PER_DOCUMENT,
    PHASE2_MAX_TABS_PER_CHUNK,
    FileChange,
    SyncAction,
    SyncTargetConfig,
)
from src.path_utils import (
    find_watch_folder,
    generate_flat_tab_title,
    is_under_watch_folder,
    is_valid_sync_file,
    relative_path_from_watch_parent,
    tab_title_for_path,
    validate_unique_titles,
)

DOC1_FOLDERS = [
    "content/copilot/get-started",
    "content/copilot/concepts",
    "content/copilot/responsible-use",
    "content/copilot/copilot-on-ghes",
]


class TestConstants:
    def test_guardrail_constants(self) -> None:
        assert MAX_TABS_PER_DOCUMENT == 100
        assert MAX_TAB_TITLE_LENGTH == 50
        assert PHASE2_MAX_TABS_PER_CHUNK == 10
        assert "タブ 1" in DEFAULT_TAB_NAMES and "Tab 1" in DEFAULT_TAB_NAMES

    def test_sync_action_is_str_enum(self) -> None:
        assert SyncAction.ADD == "ADD"

    def test_file_change_defaults(self) -> None:
        fc = FileChange(path="content/copilot/concepts/a.md", status="added")
        assert fc.raw_content is None and fc.previous_path is None

    def test_sync_target_owner_repo(self) -> None:
        target = SyncTargetConfig(repository="github/docs", watch_folders=[])
        assert (target.owner, target.repo) == ("github", "docs")


class TestIsValidSyncFile:
    @pytest.mark.parametrize(
        "name",
        [
            "content/copilot/concepts/about.md",
            "content/copilot/concepts/notes.txt",
            "content/copilot/concepts/reindex.md",  # index.md 末尾一致だがパス境界ではない
            "content/copilot/concepts/my-index.md",
        ],
    )
    def test_valid(self, name: str) -> None:
        assert is_valid_sync_file(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "index.md",
            "content/copilot/concepts/index.md",
            "content\\copilot\\concepts\\index.md",
            "content/copilot/concepts/image.png",
            "content/copilot/concepts/data.yml",
            "content/copilot/concepts/about.MDX",
        ],
    )
    def test_invalid(self, name: str) -> None:
        assert is_valid_sync_file(name) is False


class TestWatchFolder:
    def test_under_folder(self) -> None:
        assert is_under_watch_folder("content/copilot/concepts/a.md", "content/copilot/concepts")
        assert is_under_watch_folder("content/copilot/concepts/sub/a.md", "content/copilot/concepts/")
        assert is_under_watch_folder("content\\copilot\\concepts\\a.md", "content/copilot/concepts")

    def test_boundary_not_prefix_match(self) -> None:
        assert not is_under_watch_folder("content/copilot/concepts-x/a.md", "content/copilot/concepts")
        assert not is_under_watch_folder("content/copilot/concepts", "content/copilot/concepts")

    def test_find_deepest_folder(self) -> None:
        folders = ["content/copilot/tutorials", "content/copilot/tutorials/budgets"]
        assert find_watch_folder("content/copilot/tutorials/budgets/a.md", folders) == folders[1]
        assert find_watch_folder("content/copilot/tutorials/a.md", folders) == folders[0]
        assert find_watch_folder("content/copilot/reference/a.md", folders) is None


class TestTabTitle:
    def test_relative_from_parent_includes_folder_name(self) -> None:
        assert (
            relative_path_from_watch_parent("content/copilot/concepts/foo.md", "content/copilot/concepts")
            == "concepts/foo.md"
        )
        assert (
            relative_path_from_watch_parent(
                "content/copilot/how-tos/copilot-cli/a/b.md", "content/copilot/how-tos/copilot-cli"
            )
            == "copilot-cli/a/b.md"
        )

    def test_relative_outside_folder_raises(self) -> None:
        with pytest.raises(ValueError):
            relative_path_from_watch_parent("content/copilot/other/foo.md", "content/copilot/concepts")

    def test_spec_example(self) -> None:
        assert (
            generate_flat_tab_title("concepts/agent-capabilities/about-agents.md")
            == "concepts_agent-capabilities_about-agents_md"
        )

    def test_exactly_50_chars_kept(self) -> None:
        rel = "a" * 47 + ".md"  # flat = 47 + "_md" = 50
        title = generate_flat_tab_title(rel)
        assert len(title) == 50 and title == "a" * 47 + "_md"

    def test_long_title_is_shortened_deterministically(self) -> None:
        rel = "concepts/agent-capabilities/about-agents/very-long-agent-application-name.md"
        flat = rel.replace("/", "_").replace(".", "_")
        expected = flat[:41] + "_" + hashlib.sha256(flat.encode("utf-8")).hexdigest()[:8]
        title = generate_flat_tab_title(rel)
        assert title == expected
        assert len(title) == 50
        assert generate_flat_tab_title(rel) == title  # 決定論的

    def test_long_titles_with_same_prefix_do_not_collide(self) -> None:
        base = "concepts/" + "x" * 40
        t1 = generate_flat_tab_title(base + "/alpha.md")
        t2 = generate_flat_tab_title(base + "/beta.md")
        assert t1[:41] == t2[:41] and t1 != t2

    def test_tab_title_for_path(self) -> None:
        assert tab_title_for_path("content/copilot/concepts/foo.md", DOC1_FOLDERS) == "concepts_foo_md"
        assert (
            tab_title_for_path("content/copilot/get-started/quickstart.md", DOC1_FOLDERS)
            == "get-started_quickstart_md"
        )
        assert tab_title_for_path("content/copilot/reference/foo.md", DOC1_FOLDERS) is None

    def test_validate_unique_titles(self) -> None:
        validate_unique_titles({"a.md": "x", "b.md": "y"})
        with pytest.raises(RuntimeError, match="collision"):
            validate_unique_titles({"concepts/a.b.md": "concepts_a_b_md", "concepts/a_b.md": "concepts_a_b_md"})

