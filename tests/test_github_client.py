"""github_client.py の単体テスト（unittest.mock による HTTP モック。実通信なし）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from src.github_client import (
    COMPARE_FILES_LIMIT,
    GitHubAPIError,
    GitHubClient,
    build_retry,
    build_session,
    classify_compare_files,
)
from src.liquid_resolver import LiquidResolver

API = "https://api.github.com/repos/github/docs"
RAW = "https://raw.githubusercontent.com/github/docs"
DOC1 = [
    "content/copilot/get-started",
    "content/copilot/concepts",
    "content/copilot/responsible-use",
    "content/copilot/copilot-on-ghes",
]


def _resp(status: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = json_data
    r.text = text
    return r


class FakeSession:
    """URL → レスポンスのルーティングを持つ session モック。呼び出し履歴を記録する。"""

    def __init__(self, routes: dict[str, MagicMock | list[MagicMock]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.get = MagicMock(side_effect=self._get)

    def _get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> MagicMock:
        self.calls.append((url, params))
        route = self.routes.get(url)
        if route is None:
            return _resp(404, text="Not Found")
        if isinstance(route, list):
            return route.pop(0)
        return route


def _client(routes: dict[str, Any]) -> tuple[GitHubClient, FakeSession]:
    session = FakeSession(routes)
    return GitHubClient("github/docs", session=session), session  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Session / Retry
# ---------------------------------------------------------------------------


class TestSession:
    def test_retry_configuration(self) -> None:
        retry = build_retry()
        assert retry.total == 3
        assert retry.backoff_factor == 1
        assert 429 in retry.status_forcelist and 503 in retry.status_forcelist

    def test_session_mounts_retry_adapter(self) -> None:
        session = build_session("tok")
        adapter = session.get_adapter("https://api.github.com")
        assert adapter.max_retries.total == 3
        assert adapter.max_retries.backoff_factor == 1
        assert session.headers["Authorization"] == "Bearer tok"

    def test_session_without_token(self) -> None:
        assert "Authorization" not in build_session(None).headers

    def test_connection_error_wrapped(self) -> None:
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("RemoteDisconnected")
        client = GitHubClient("github/docs", session=session)
        with pytest.raises(GitHubAPIError, match="RemoteDisconnected"):
            client.get_head_sha()

    def test_http_error_logged_and_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        client, _ = _client({f"{API}/commits/main": _resp(500, text="boom")})
        with pytest.raises(GitHubAPIError) as exc_info:
            client.get_head_sha()
        assert exc_info.value.status_code == 500
        assert "status=500" in caplog.text and "boom" in caplog.text


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


class TestClassifyCompareFiles:
    def test_index_md_and_non_md_excluded(self) -> None:
        files = [
            {"filename": "content/copilot/concepts/index.md", "status": "modified"},
            {"filename": "content/copilot/concepts/agents/index.md", "status": "added"},
            {"filename": "content/copilot/concepts/image.png", "status": "added"},
            {"filename": "content/copilot/concepts/about.md", "status": "modified"},
        ]
        changes = classify_compare_files(files, DOC1)
        assert [(c.path, c.status) for c in changes] == [("content/copilot/concepts/about.md", "modified")]

    def test_outside_watch_folders_excluded(self) -> None:
        files = [
            {"filename": "content/copilot/reference/a.md", "status": "added"},
            {"filename": "content/copilot/concepts-old/a.md", "status": "added"},
            {"filename": "data/reusables/copilot/x.md", "status": "modified"},
        ]
        assert classify_compare_files(files, DOC1) == []

    def test_status_mapping(self) -> None:
        files = [
            {"filename": "content/copilot/concepts/a.md", "status": "added"},
            {"filename": "content/copilot/concepts/b.md", "status": "removed"},
            {"filename": "content/copilot/concepts/c.md", "status": "copied"},
            {"filename": "content/copilot/concepts/d.md", "status": "changed"},
        ]
        assert [c.status for c in classify_compare_files(files, DOC1)] == ["added", "removed", "added", "modified"]

    def test_renamed_within_watch(self) -> None:
        files = [{
            "filename": "content/copilot/concepts/new.md",
            "previous_filename": "content/copilot/get-started/old.md",
            "status": "renamed",
        }]
        (c,) = classify_compare_files(files, DOC1)
        assert (c.path, c.status, c.previous_path) == (
            "content/copilot/concepts/new.md", "renamed", "content/copilot/get-started/old.md",
        )

    def test_renamed_out_of_watch_becomes_removed(self) -> None:
        files = [{
            "filename": "content/copilot/reference/moved.md",
            "previous_filename": "content/copilot/concepts/moved.md",
            "status": "renamed",
        }]
        (c,) = classify_compare_files(files, DOC1)
        assert (c.path, c.status) == ("content/copilot/concepts/moved.md", "removed")

    def test_renamed_into_watch_becomes_added(self) -> None:
        files = [{
            "filename": "content/copilot/concepts/moved.md",
            "previous_filename": "content/copilot/reference/moved.md",
            "status": "renamed",
        }]
        (c,) = classify_compare_files(files, DOC1)
        assert (c.path, c.status) == ("content/copilot/concepts/moved.md", "added")

    def test_renamed_from_index_md(self) -> None:
        files = [{
            "filename": "content/copilot/concepts/overview.md",
            "previous_filename": "content/copilot/concepts/index.md",
            "status": "renamed",
        }]
        (c,) = classify_compare_files(files, DOC1)
        assert c.status == "added"


class TestCompare:
    def test_compare_paginates_and_flags_data_change(self) -> None:
        page1 = [{"filename": f"content/copilot/concepts/f{i}.md", "status": "modified"} for i in range(100)]
        page2 = [
            {"filename": "content/copilot/concepts/index.md", "status": "modified"},
            {"filename": "data/variables/product.yml", "status": "modified"},
        ]
        url = f"{API}/compare/aaa...bbb"
        client, session = _client({url: [_resp(json_data={"files": page1}), _resp(json_data={"files": page2})]})
        result = client.compare("aaa", "bbb", DOC1)
        assert len(result.changes) == 100
        assert result.data_changed is True
        assert result.truncated is False
        assert [c[1]["page"] for c in session.calls] == [1, 2]

    def test_compare_truncated_at_limit(self, caplog: pytest.LogCaptureFixture) -> None:
        pages = [
            _resp(json_data={"files": [{"filename": f"x/{p}-{i}.md", "status": "added"} for i in range(100)]})
            for p in range(3)
        ]
        client, session = _client({f"{API}/compare/a...b": pages})
        result = client.compare("a", "b", DOC1)
        assert result.truncated is True
        assert len(session.calls) == COMPARE_FILES_LIMIT // 100
        assert "may be incomplete" in caplog.text

    def test_head_sha(self) -> None:
        client, _ = _client({f"{API}/commits/main": _resp(json_data={"sha": "abc123"})})
        assert client.get_head_sha() == "abc123"


# ---------------------------------------------------------------------------
# Folder listing
# ---------------------------------------------------------------------------


class TestListFolderFiles:
    def _routes(self, truncated: bool = False) -> dict[str, Any]:
        return {
            f"{API}/contents/content/copilot": _resp(json_data=[
                {"name": "concepts", "type": "dir", "sha": "tree-concepts"},
                {"name": "concepts-x", "type": "dir", "sha": "tree-x"},
                {"name": "index.md", "type": "file", "sha": "f"},
            ]),
            f"{API}/git/trees/tree-concepts": _resp(json_data={
                "truncated": truncated,
                "tree": [
                    {"path": "index.md", "type": "blob"},
                    {"path": "about.md", "type": "blob"},
                    {"path": "agents", "type": "tree"},
                    {"path": "agents/index.md", "type": "blob"},
                    {"path": "agents/coding-agent.md", "type": "blob"},
                    {"path": "agents/diagram.png", "type": "blob"},
                ],
            }),
        }

    def test_lists_subtree_excluding_index(self) -> None:
        client, session = _client(self._routes())
        files = client.list_folder_files("content/copilot/concepts", "sha1")
        assert files == ["content/copilot/concepts/about.md", "content/copilot/concepts/agents/coding-agent.md"]
        assert session.calls[0][1] == {"ref": "sha1"}
        assert session.calls[1][1] == {"recursive": "1"}

    def test_missing_folder_raises(self) -> None:
        client, _ = _client(self._routes())
        with pytest.raises(GitHubAPIError, match="not found"):
            client.list_folder_files("content/copilot/vanished", "sha1")

    def test_truncated_tree_raises(self) -> None:
        client, _ = _client(self._routes(truncated=True))
        with pytest.raises(GitHubAPIError, match="truncated"):
            client.list_folder_files("content/copilot/concepts", "sha1")


# ---------------------------------------------------------------------------
# Raw content & data/ resources
# ---------------------------------------------------------------------------


class TestRawAndData:
    def test_fetch_raw_cached_and_404(self) -> None:
        url = f"{RAW}/sha1/content/copilot/concepts/a.md"
        client, session = _client({url: _resp(text="# A")})
        assert client.fetch_raw("content/copilot/concepts/a.md", "sha1") == "# A"
        assert client.fetch_raw("content/copilot/concepts/a.md", "sha1") == "# A"
        assert client.fetch_raw("content/copilot/concepts/missing.md", "sha1") is None
        assert client.fetch_raw("content/copilot/concepts/missing.md", "sha1") is None
        assert len(session.calls) == 2

    def test_cache_is_per_ref(self) -> None:
        client, _ = _client({
            f"{RAW}/sha1/data/reusables/a.md": _resp(text="old"),
            f"{RAW}/sha2/data/reusables/a.md": _resp(text="new"),
        })
        assert client.fetch_raw("data/reusables/a.md", "sha1") == "old"
        assert client.fetch_raw("data/reusables/a.md", "sha2") == "new"

    def test_fetch_contents_skips_removed_and_detects_missing(self) -> None:
        from src.models import FileChange

        client, _ = _client({f"{RAW}/sha1/content/copilot/concepts/a.md": _resp(text="A")})
        changes = [
            FileChange("content/copilot/concepts/a.md", "modified"),
            FileChange("content/copilot/concepts/gone.md", "removed"),
        ]
        client.fetch_contents(changes, "sha1")
        assert changes[0].raw_content == "A" and changes[1].raw_content is None
        with pytest.raises(GitHubAPIError, match="not found"):
            client.fetch_contents([FileChange("content/copilot/concepts/x.md", "added")], "sha1")

    def test_load_variables_cached(self) -> None:
        client, session = _client({
            f"{API}/contents/data/variables": _resp(json_data=[
                {"name": "product.yml", "type": "file"},
                {"name": "copilot.yml", "type": "file"},
                {"name": "README.md", "type": "file"},
                {"name": "sub", "type": "dir"},
            ]),
            f"{RAW}/sha1/data/variables/product.yml": _resp(text="prodname_copilot: GitHub Copilot\n"),
            f"{RAW}/sha1/data/variables/copilot.yml": _resp(text="copilot_chat_short: Copilot Chat\nn: 3\n"),
        })
        v = client.load_variables("sha1")
        assert v == {"product": {"prodname_copilot": "GitHub Copilot"}, "copilot": {"copilot_chat_short": "Copilot Chat", "n": "3"}}
        calls_before = len(session.calls)
        assert client.load_variables("sha1") is v
        assert len(session.calls) == calls_before

    def test_load_variables_invalid_yaml(self) -> None:
        client, _ = _client({
            f"{API}/contents/data/variables": _resp(json_data=[{"name": "bad.yml", "type": "file"}]),
            f"{RAW}/sha1/data/variables/bad.yml": _resp(text="a: [unclosed"),
        })
        with pytest.raises(GitHubAPIError, match="Invalid YAML"):
            client.load_variables("sha1")

    def test_reusable_loader_integrates_with_resolver(self) -> None:
        client, session = _client({
            f"{API}/contents/data/variables": _resp(json_data=[{"name": "product.yml", "type": "file"}]),
            f"{RAW}/sha1/data/variables/product.yml": _resp(text="prodname_copilot: GitHub Copilot\n"),
            f"{RAW}/sha1/data/reusables/copilot/about.md": _resp(text="{% data variables.product.prodname_copilot %} rocks.\n"),
        })
        resolver = LiquidResolver(client.make_reusable_loader("sha1"), client.load_variables("sha1"))
        out = resolver.resolve("{% data reusables.copilot.about %}\n{% data reusables.copilot.about %}")
        assert out == "GitHub Copilot rocks.\nGitHub Copilot rocks.\n"
        reusable_calls = [c for c in session.calls if "reusables" in c[0]]
        assert len(reusable_calls) == 1

    def test_reusable_loader_network_error_propagates(self) -> None:
        client, _ = _client({f"{RAW}/sha1/data/reusables/a/b.md": _resp(502, text="bad gateway")})
        resolver = LiquidResolver(client.make_reusable_loader("sha1"), {})
        with pytest.raises(GitHubAPIError):
            resolver.resolve("{% data reusables.a.b %}")
