"""config.yaml パーサー・検証。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from src.models import DocumentGroup, SyncTargetConfig, WatchFolderConfig
from src.path_utils import is_under_watch_folder, normalize_path

logger = logging.getLogger(__name__)

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REQUIRED_FOLDER_KEYS = ("source_path", "google_doc_id", "doc_name")


class ConfigError(ValueError):
    """config.yaml の構造・値が不正な場合に送出される。"""


def _require_str(value: Any, key: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}: '{key}' must be a non-empty string (got {value!r})")
    return value.strip()


def parse_config(data: Any) -> list[SyncTargetConfig]:
    """YAML ロード済みの辞書から SyncTargetConfig のリストを生成・検証する。"""
    if not isinstance(data, dict) or "sync_targets" not in data:
        raise ConfigError("config root must be a mapping containing 'sync_targets'")

    raw_targets = data["sync_targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("'sync_targets' must be a non-empty list")

    targets: list[SyncTargetConfig] = []
    for t_idx, raw_target in enumerate(raw_targets):
        ctx = f"sync_targets[{t_idx}]"
        if not isinstance(raw_target, dict):
            raise ConfigError(f"{ctx} must be a mapping")

        repository = _require_str(raw_target.get("repository"), "repository", ctx)
        if not _REPOSITORY_PATTERN.match(repository):
            raise ConfigError(f"{ctx}: repository must be 'owner/repo' format (got {repository!r})")

        raw_folders = raw_target.get("watch_folders")
        if not isinstance(raw_folders, list) or not raw_folders:
            raise ConfigError(f"{ctx}: 'watch_folders' must be a non-empty list")

        folders: list[WatchFolderConfig] = []
        seen_paths: set[str] = set()
        doc_names: dict[str, str] = {}
        for f_idx, raw_folder in enumerate(raw_folders):
            fctx = f"{ctx}.watch_folders[{f_idx}]"
            if not isinstance(raw_folder, dict):
                raise ConfigError(f"{fctx} must be a mapping")
            values = {k: _require_str(raw_folder.get(k), k, fctx) for k in _REQUIRED_FOLDER_KEYS}
            source_path = normalize_path(values["source_path"])
            if source_path in seen_paths:
                raise ConfigError(f"{fctx}: duplicated source_path {source_path!r}")
            seen_paths.add(source_path)

            doc_id = values["google_doc_id"]
            if doc_id in doc_names and doc_names[doc_id] != values["doc_name"]:
                raise ConfigError(
                    f"{fctx}: google_doc_id {doc_id!r} has conflicting doc_name "
                    f"({doc_names[doc_id]!r} vs {values['doc_name']!r})"
                )
            doc_names[doc_id] = values["doc_name"]
            folders.append(WatchFolderConfig(source_path, doc_id, values["doc_name"]))

        _warn_nested_folders(folders)
        targets.append(SyncTargetConfig(repository=repository, watch_folders=folders))

    return targets


def _warn_nested_folders(folders: list[WatchFolderConfig]) -> None:
    """監視フォルダ同士の包含関係を検出して警告する（最も深いフォルダが優先される）。"""
    for outer in folders:
        for inner in folders:
            if outer is not inner and is_under_watch_folder(inner.source_path, outer.source_path):
                logger.warning(
                    "Watch folder %r is nested under %r; files will be assigned to the deepest folder.",
                    inner.source_path,
                    outer.source_path,
                )


def load_config(path: str | Path = "config.yaml") -> list[SyncTargetConfig]:
    """config.yaml を読み込み、検証済みの SyncTargetConfig リストを返す。"""
    config_path = Path(path)
    try:
        with config_path.open(encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    return parse_config(data)


def group_by_document(target: SyncTargetConfig) -> list[DocumentGroup]:
    """同一 google_doc_id の監視フォルダを 1 つの DocumentGroup に集約する（定義順を保持）。"""
    groups: dict[str, DocumentGroup] = {}
    for folder in target.watch_folders:
        group = groups.get(folder.google_doc_id)
        if group is None:
            group = DocumentGroup(google_doc_id=folder.google_doc_id, doc_name=folder.doc_name)
            groups[folder.google_doc_id] = group
        group.watch_folders.append(folder)
    return list(groups.values())

