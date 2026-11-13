"""config.py の単体テスト。"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import pytest

from src.config import ConfigError, group_by_document, load_config, parse_config

DOC1_ID = "1AMOPbDQnIoJiKbhDc-7Qt4zQMqyqkHj9VyiDAs1LbQg"

VALID: dict[str, Any] = {
    "sync_targets": [
        {
            "repository": "github/docs",
            "watch_folders": [
                {"source_path": "content/copilot/concepts/", "google_doc_id": "DOC1", "doc_name": "Doc 1"},
                {"source_path": "content/copilot/get-started", "google_doc_id": "DOC1", "doc_name": "Doc 1"},
                {"source_path": "content/copilot/reference", "google_doc_id": "DOC8", "doc_name": "Doc 8"},
            ],
        }
    ]
}


def _mutated(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    folder = data["sync_targets"][0]["watch_folders"][0]
    folder.update(changes)
    return data


class TestRealConfig:
    def test_doc1_config(self, repo_root: Path) -> None:
        targets = load_config(repo_root / "config.yaml")
        assert len(targets) == 1
        target = targets[0]
        assert target.repository == "github/docs"
        assert [wf.source_path for wf in target.watch_folders[:4]] == [
            "content/copilot/get-started",
            "content/copilot/concepts",
            "content/copilot/responsible-use",
            "content/copilot/copilot-on-ghes",
        ]
        groups = group_by_document(target)
        assert len(groups) >= 1
        assert groups[0].google_doc_id == DOC1_ID
        assert groups[0].doc_name == "[Copilot Docs] 01 - Concepts & Getting Started"
        assert len(groups[0].watch_folders) == 4


class TestParseConfig:
    def test_valid_and_normalized(self) -> None:
        targets = parse_config(VALID)
        assert targets[0].watch_folders[0].source_path == "content/copilot/concepts"

    def test_grouping_preserves_order(self) -> None:
        groups = group_by_document(parse_config(VALID)[0])
        assert [g.google_doc_id for g in groups] == ["DOC1", "DOC8"]
        assert groups[0].source_paths == ["content/copilot/concepts", "content/copilot/get-started"]

    @pytest.mark.parametrize(
        "data",
        [
            None,
            {},
            {"sync_targets": []},
            {"sync_targets": ["not-a-mapping"]},
            {"sync_targets": [{"repository": "github/docs", "watch_folders": []}]},
        ],
    )
    def test_invalid_structure(self, data: Any) -> None:
        with pytest.raises(ConfigError):
            parse_config(data)

    @pytest.mark.parametrize("repo", ["github", "github/docs/extra", "", "git hub/docs"])
    def test_invalid_repository(self, repo: str) -> None:
        data = copy.deepcopy(VALID)
        data["sync_targets"][0]["repository"] = repo
        with pytest.raises(ConfigError):
            parse_config(data)

    @pytest.mark.parametrize("key", ["source_path", "google_doc_id", "doc_name"])
    def test_missing_or_empty_folder_keys(self, key: str) -> None:
        with pytest.raises(ConfigError, match=key):
            parse_config(_mutated(**{key: ""}))
        data = copy.deepcopy(VALID)
        del data["sync_targets"][0]["watch_folders"][0][key]
        with pytest.raises(ConfigError, match=key):
            parse_config(data)

    def test_duplicated_source_path(self) -> None:
        with pytest.raises(ConfigError, match="duplicated"):
            parse_config(_mutated(source_path="content/copilot/get-started/"))

    def test_conflicting_doc_name(self) -> None:
        with pytest.raises(ConfigError, match="conflicting doc_name"):
            parse_config(_mutated(doc_name="Another name"))

    def test_nested_folders_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        data = copy.deepcopy(VALID)
        data["sync_targets"][0]["watch_folders"].append(
            {"source_path": "content/copilot/concepts/agents", "google_doc_id": "DOC1", "doc_name": "Doc 1"}
        )
        with caplog.at_level(logging.WARNING):
            parse_config(data)
        assert "nested" in caplog.text


class TestLoadConfig:
    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "missing.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("sync_targets: [\n  - {", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config(p)

