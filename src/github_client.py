"""GitHub API クライアント（Compare / Contents / Git Trees / raw 取得）。

- 差分同期: Compare API で監視フォルダ内の変更ファイルを抽出する。
- 全量同期: 監視フォルダのサブツリーのみを Git Trees API (recursive=1) で列挙する。
  （github/docs のルートツリーは巨大で `truncated` になるため、ルートからの再帰取得は行わない）
- 本文取得: raw.githubusercontent.com から取得する（API レート制限を消費しない）。
- data/reusables / data/variables はコミット SHA 単位でメモリキャッシュする。
- index.md は is_valid_sync_file により完全除外する。
"""

from __future__ import annotations

import logging
import os
import posixpath
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.liquid_resolver import ReusableLoader, VariablesDict, normalize_variables
from src.models import CompareResult, FileChange
from src.path_utils import find_watch_folder, is_valid_sync_file, normalize_path

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_RAW_BASE = "https://raw.githubusercontent.com"
#: Compare API が返すファイル数の上限。到達した場合は差分の取りこぼし可能性がある
COMPARE_FILES_LIMIT = 300
REUSABLES_DIR = "data/reusables"
VARIABLES_DIR = "data/variables"
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)

_STATUS_MAP = {
    "added": "added",
    "copied": "added",
    "modified": "modified",
    "changed": "modified",
    "removed": "removed",
}


class GitHubAPIError(RuntimeError):
    """GitHub API 呼び出し失敗、または取得結果の整合性異常。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_retry() -> Retry:
    """通信切断（RemoteDisconnected 等）・レート制限・5xx に備えたリトライ設定。"""
    return Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )


def build_session(token: str | None = None) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=build_retry())
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "github-copilot-notebook-sync"})
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def classify_compare_files(files: Iterable[dict[str, Any]], watch_folders: list[str]) -> list[FileChange]:
    """
    Compare API の files[] を FileChange に変換する。
    - is_valid_sync_file（index.md 除外・拡張子判定）と監視フォルダ所属で厳格にフィルタする。
    - renamed は新旧パスの所属に応じて renamed / added / removed に分類する。
    """
    changes: list[FileChange] = []
    for entry in files:
        path = normalize_path(entry.get("filename", ""))
        status = entry.get("status", "")
        new_in = bool(path) and is_valid_sync_file(path) and find_watch_folder(path, watch_folders) is not None

        if status == "renamed":
            prev = normalize_path(entry.get("previous_filename") or "")
            old_in = bool(prev) and is_valid_sync_file(prev) and find_watch_folder(prev, watch_folders) is not None
            if new_in and old_in:
                changes.append(FileChange(path=path, status="renamed", previous_path=prev))
            elif new_in:
                changes.append(FileChange(path=path, status="added"))
            elif old_in:
                changes.append(FileChange(path=prev, status="removed"))
            continue

        if not new_in:
            continue
        mapped = _STATUS_MAP.get(status)
        if mapped is None:
            logger.warning("Unknown compare status %r for %s; treated as modified.", status, path)
            mapped = "modified"
        changes.append(FileChange(path=path, status=mapped))
    return changes


def _touches_data(files: Iterable[dict[str, Any]]) -> bool:
    prefixes = (REUSABLES_DIR + "/", VARIABLES_DIR + "/")
    for entry in files:
        for key in ("filename", "previous_filename"):
            value = normalize_path(entry.get(key) or "")
            if value.startswith(prefixes):
                return True
    return False


class GitHubClient:
    def __init__(
        self,
        repository: str,
        token: str | None = None,
        session: requests.Session | None = None,
        api_base: str = DEFAULT_API_BASE,
        raw_base: str = DEFAULT_RAW_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.repository = repository
        self.session = session if session is not None else build_session(token or os.environ.get("GITHUB_TOKEN"))
        self.api_base = api_base.rstrip("/")
        self.raw_base = raw_base.rstrip("/")
        self.timeout = timeout
        self._raw_cache: dict[tuple[str, str], str | None] = {}
        self._variables_cache: dict[str, VariablesDict] = {}

    # ------------------------------------------------------------ HTTP helper

    def _get(self, url: str, params: dict[str, Any] | None = None, allow_404: bool = False) -> requests.Response | None:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.error("GitHub request failed: GET %s params=%s error=%s", url, params, exc)
            raise GitHubAPIError(f"GET {url} failed: {exc}") from exc
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            logger.error(
                "GitHub API error: GET %s params=%s status=%s body=%s",
                url, params, response.status_code, response.text[:2000],
            )
            raise GitHubAPIError(f"GET {url} returned HTTP {response.status_code}", response.status_code)
        return response

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._get(f"{self.api_base}/repos/{self.repository}/{path.lstrip('/')}", params)
        assert response is not None
        return response.json()

    # ---------------------------------------------------------------- commits

    def get_head_sha(self, branch: str = "main") -> str:
        data = self._get_json(f"commits/{quote(branch, safe='')}")
        sha = data.get("sha") if isinstance(data, dict) else None
        if not sha:
            raise GitHubAPIError(f"Could not determine head SHA of {self.repository}@{branch}")
        return sha

    def compare(self, base: str, head: str, watch_folders: list[str]) -> CompareResult:
        """base...head の差分から監視フォルダ内の有効な変更を抽出する。"""
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get_json(f"compare/{base}...{head}", params={"per_page": 100, "page": page})
            page_files = data.get("files") or []
            files.extend(page_files)
            if len(page_files) < 100 or len(files) >= COMPARE_FILES_LIMIT:
                break
            page += 1

        truncated = len(files) >= COMPARE_FILES_LIMIT
        if truncated:
            logger.warning(
                "Compare %s...%s returned %d files (limit %d); diff may be incomplete.",
                base, head, len(files), COMPARE_FILES_LIMIT,
            )
        return CompareResult(
            changes=classify_compare_files(files, watch_folders),
            truncated=truncated,
            data_changed=_touches_data(files),
        )

    # ------------------------------------------------------------ tree listing

    def list_directory(self, path: str, ref: str) -> list[dict[str, Any]]:
        data = self._get_json(f"contents/{quote(normalize_path(path))}", params={"ref": ref})
        if not isinstance(data, list):
            raise GitHubAPIError(f"'{path}' is not a directory in {self.repository}@{ref}")
        return data

    def list_folder_files(self, folder: str, ref: str) -> list[str]:
        """監視フォルダ配下の有効な同期対象ファイル（index.md 除外済み）をフルパスで返す。"""
        folder = normalize_path(folder)
        parent, name = posixpath.split(folder)
        entries = self.list_directory(parent, ref) if parent else self._root_entries(ref)
        tree_sha = next((e.get("sha") for e in entries if e.get("name") == name and e.get("type") == "dir"), None)
        if not tree_sha:
            # フォルダ消失時に全タブを孤立判定させないため、空リストではなく例外とする
            raise GitHubAPIError(f"Watch folder '{folder}' not found in {self.repository}@{ref}")

        tree = self._get_json(f"git/trees/{tree_sha}", params={"recursive": "1"})
        if tree.get("truncated"):
            raise GitHubAPIError(f"Git tree for '{folder}' is truncated; cannot guarantee a complete listing")

        files = [
            f"{folder}/{item['path']}"
            for item in tree.get("tree", [])
            if item.get("type") == "blob" and is_valid_sync_file(f"{folder}/{item['path']}")
        ]
        return sorted(files)

    def _root_entries(self, ref: str) -> list[dict[str, Any]]:
        data = self._get_json("contents", params={"ref": ref})
        return data if isinstance(data, list) else []

    # ----------------------------------------------------------- raw content

    def fetch_raw(self, path: str, ref: str) -> str | None:
        """raw ファイル本文を取得する（404 は None）。(ref, path) 単位でキャッシュする。"""
        key = (ref, normalize_path(path))
        if key not in self._raw_cache:
            url = f"{self.raw_base}/{self.repository}/{quote(ref, safe='')}/{quote(key[1])}"
            response = self._get(url, allow_404=True)
            if response is None:
                self._raw_cache[key] = None
            else:
                response.encoding = "utf-8"
                self._raw_cache[key] = response.text
        return self._raw_cache[key]

    def fetch_contents(self, changes: list[FileChange], ref: str) -> list[FileChange]:
        """removed 以外の FileChange に raw_content を設定する。取得できない場合は整合性異常として例外。"""
        for change in changes:
            if change.status == "removed":
                continue
            content = self.fetch_raw(change.path, ref)
            if content is None:
                raise GitHubAPIError(f"File '{change.path}' not found at {ref} (status={change.status})")
            change.raw_content = content
        return changes

    # ------------------------------------------------------- data/ resources

    def make_reusable_loader(self, ref: str) -> ReusableLoader:
        """LiquidResolver に注入する Reusables ローダー（fetch_raw のキャッシュを共有）。"""

        def _load(rel_path: str) -> str | None:
            return self.fetch_raw(f"{REUSABLES_DIR}/{rel_path}.md", ref)

        return _load

    def load_variables(self, ref: str) -> VariablesDict:
        """data/variables/*.yml をロードし、コミット SHA 単位でキャッシュする。"""
        if ref in self._variables_cache:
            return self._variables_cache[ref]
        raw_files: dict[str, Any] = {}
        for entry in self.list_directory(VARIABLES_DIR, ref):
            name = entry.get("name", "")
            if entry.get("type") != "file" or not name.endswith((".yml", ".yaml")):
                continue
            text = self.fetch_raw(f"{VARIABLES_DIR}/{name}", ref)
            if text is None:
                raise GitHubAPIError(f"Variables file '{name}' disappeared at {ref}")
            try:
                raw_files[name.rsplit(".", 1)[0]] = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                logger.error("Failed to parse variables file %s: %s", name, exc)
                raise GitHubAPIError(f"Invalid YAML in {VARIABLES_DIR}/{name}") from exc
        variables = normalize_variables(raw_files)
        self._variables_cache[ref] = variables
        logger.info("Loaded %d variables files at %s", len(variables), ref[:12])
        return variables
