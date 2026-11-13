"""GitHub Copilot Docs ↔ Google Docs Tabs 同期オーケストレーター。

Usage:
    python src/main.py [--force] [--dry-run] [--doc-id <GOOGLE_DOC_ID>] [--config config.yaml]
    python -m src.main [--force] [--dry-run] [--doc-id <GOOGLE_DOC_ID>] [--config config.yaml]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

# `python src/main.py` として直接実行された場合でも `src.*` を解決できるようにする
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.config import ConfigError, group_by_document, load_config  # noqa: E402
from src.docs_client import GoogleDocsClient  # noqa: E402
from src.github_client import GitHubClient  # noqa: E402
from src.liquid_resolver import LiquidResolver  # noqa: E402
from src.models import DocumentGroup, DocumentSyncReport, FileChange, SyncTargetConfig  # noqa: E402
from src.path_utils import find_watch_folder, tab_title_for_path, validate_unique_titles  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_CREDENTIALS_PATH = "credentials.json"
DEFAULT_DRY_RUN_DIR = "out/dry_run"


def commit_sha_cache_path(target: SyncTargetConfig, cache_dir: str | Path = ".") -> Path:
    """`last_commit_sha_{owner}_{repo}.txt` のパスを返す。"""
    return Path(cache_dir) / f"last_commit_sha_{target.owner}_{target.repo}.txt"


def read_cached_sha(path: Path) -> str | None:
    if not path.is_file():
        return None
    sha = path.read_text(encoding="utf-8").strip()
    return sha or None


def write_cached_sha(path: Path, sha: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{sha.strip()}\n", encoding="utf-8")


def scope_changes_to_group(changes: list[FileChange], group_folders: list[str]) -> list[FileChange]:
    """変更一覧を単一 DocumentGroup の監視フォルダ群にスコープする（分冊間リネームにも対応）。"""
    scoped: list[FileChange] = []
    for change in changes:
        new_in = find_watch_folder(change.path, group_folders) is not None
        if change.status == "renamed":
            prev = change.previous_path or ""
            old_in = bool(prev) and find_watch_folder(prev, group_folders) is not None
            if new_in and old_in:
                scoped.append(FileChange(path=change.path, status="renamed", previous_path=prev))
            elif new_in:
                scoped.append(FileChange(path=change.path, status="added"))
            elif old_in:
                scoped.append(FileChange(path=prev, status="removed"))
        elif new_in:
            scoped.append(FileChange(path=change.path, status=change.status))
    return scoped


def write_dry_run_outputs(
    output_dir: str | Path,
    google_doc_id: str,
    contents: dict[str, str],
) -> Path:
    """解決済み Markdown を `<output_dir>/<google_doc_id>/<tab_title>.md` に出力する。"""
    doc_dir = Path(output_dir) / google_doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    for tab_title, body in contents.items():
        (doc_dir / f"{tab_title}.md").write_text(body, encoding="utf-8")
    return doc_dir


def _collect_full_sync_payload(
    group: DocumentGroup,
    head_sha: str,
    gh_client: GitHubClient,
    resolver: LiquidResolver,
) -> tuple[dict[str, str], list[str]]:
    """全量同期用にグループ配下の全有効ファイルを取得・Liquid 解決し ({tab_title: body}, []) を返す。"""
    file_paths: list[str] = []
    for wf in group.watch_folders:
        for path in gh_client.list_folder_files(wf.source_path, head_sha):
            if path not in file_paths:
                file_paths.append(path)

    path_to_title: dict[str, str] = {}
    for path in file_paths:
        title = tab_title_for_path(path, group.source_paths)
        if title is None:
            continue
        path_to_title[path] = title

    validate_unique_titles(path_to_title)

    changes = [FileChange(path=p, status="added") for p in path_to_title]
    gh_client.fetch_contents(changes, head_sha)

    contents: dict[str, str] = {}
    for change in changes:
        title = path_to_title[change.path]
        contents[title] = resolver.resolve_document(change.raw_content or "", source_path=change.path)

    return contents, []


def _collect_incremental_payload(
    group: DocumentGroup,
    scoped_changes: list[FileChange],
    head_sha: str,
    gh_client: GitHubClient,
    resolver: LiquidResolver,
) -> tuple[dict[str, str], list[str]]:
    """差分同期用に ({更新タブ名: 解決済み本文}, [削除対象タブ名]) を構築する。"""
    active_changes = [c for c in scoped_changes if c.status != "removed"]
    path_to_title: dict[str, str] = {}
    for change in active_changes:
        title = tab_title_for_path(change.path, group.source_paths)
        if title is not None:
            path_to_title[change.path] = title

    validate_unique_titles(path_to_title)
    gh_client.fetch_contents(active_changes, head_sha)

    contents: dict[str, str] = {}
    for change in active_changes:
        title = path_to_title[change.path]
        contents[title] = resolver.resolve_document(change.raw_content or "", source_path=change.path)

    delete_titles: list[str] = []
    for change in scoped_changes:
        if change.status == "removed":
            old_title = tab_title_for_path(change.path, group.source_paths)
            if old_title and old_title not in contents:
                delete_titles.append(old_title)
        elif change.status == "renamed" and change.previous_path:
            old_title = tab_title_for_path(change.previous_path, group.source_paths)
            if old_title and old_title not in contents:
                delete_titles.append(old_title)

    return contents, list(dict.fromkeys(delete_titles))


def sync_target(
    target: SyncTargetConfig,
    gh_client: GitHubClient,
    docs_client: GoogleDocsClient,
    *,
    force: bool = False,
    dry_run: bool = False,
    doc_id_filter: str | None = None,
    cache_dir: str | Path = ".",
    output_dir: str | Path = DEFAULT_DRY_RUN_DIR,
) -> list[DocumentSyncReport]:
    """単一 SyncTargetConfig（例: github/docs）の同期を実行し、各ドキュメントのレポートを返す。"""
    all_groups = group_by_document(target)
    if doc_id_filter:
        groups = [g for g in all_groups if g.google_doc_id == doc_id_filter]
        if not groups:
            raise ConfigError(f"google_doc_id {doc_id_filter!r} not found in config for {target.repository}")
    else:
        groups = all_groups

    head_sha = gh_client.get_head_sha()
    sha_file = commit_sha_cache_path(target, cache_dir)
    base_sha = read_cached_sha(sha_file)

    effective_force = force
    if not effective_force and base_sha is None:
        logger.info("No commit SHA cache found (%s); running initial full sync.", sha_file)
        effective_force = True

    if not effective_force and base_sha == head_sha:
        logger.info("Repository %s is already up to date at %s.", target.repository, head_sha[:12])
        return []

    compare_changes: list[FileChange] = []
    if not effective_force:
        assert base_sha is not None
        watch_paths = [wf.source_path for g in groups for wf in g.watch_folders]
        compare_result = gh_client.compare(base_sha, head_sha, watch_paths)
        if compare_result.truncated or compare_result.data_changed:
            reason = "compare limit reached" if compare_result.truncated else "data/ resources modified"
            logger.info("Falling back to full sync (%s between %s and %s).", reason, base_sha[:12], head_sha[:12])
            effective_force = True
        else:
            compare_changes = compare_result.changes

    resolver: LiquidResolver | None = None

    def _get_resolver() -> LiquidResolver:
        nonlocal resolver
        if resolver is None:
            variables = gh_client.load_variables(head_sha)
            resolver = LiquidResolver(gh_client.make_reusable_loader(head_sha), variables)
        return resolver

    reports: list[DocumentSyncReport] = []
    for group in groups:
        logger.info("Processing %s (%s) [force=%s, dry_run=%s]", group.doc_name, group.google_doc_id, effective_force, dry_run)
        if effective_force:
            contents, delete_titles = _collect_full_sync_payload(group, head_sha, gh_client, _get_resolver())
            all_valid_titles: list[str] | None = list(contents.keys())
        else:
            scoped = scope_changes_to_group(compare_changes, group.source_paths)
            if not scoped:
                logger.info("No changes for %s (%s).", group.doc_name, group.google_doc_id)
                continue
            need_resolver = any(c.status != "removed" for c in scoped)
            group_resolver = _get_resolver() if need_resolver else LiquidResolver(lambda _: None, {})
            contents, delete_titles = _collect_incremental_payload(group, scoped, head_sha, gh_client, group_resolver)
            all_valid_titles = None

        if dry_run and contents:
            out_path = write_dry_run_outputs(output_dir, group.google_doc_id, contents)
            logger.info("[dry-run] Wrote %d resolved markdown files to %s", len(contents), out_path)

        report = docs_client.sync_document(
            group.google_doc_id,
            contents,
            delete_titles=delete_titles,
            force=effective_force,
            dry_run=dry_run,
            all_valid_titles=all_valid_titles,
        )
        reports.append(report)

    # 全グループが正常完了し、dry-run ではなく、ターゲット内の全グループが対象だった場合のみ SHA を更新
    if not dry_run and len(groups) == len(all_groups):
        write_cached_sha(sha_file, head_sha)
        logger.info("Updated commit SHA cache %s -> %s", sha_file, head_sha[:12])

    return reports


def run_sync(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    credentials_path: str | Path | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    doc_id_filter: str | None = None,
    cache_dir: str | Path = ".",
    output_dir: str | Path = DEFAULT_DRY_RUN_DIR,
    gh_client_factory: Callable[[str], GitHubClient] | None = None,
    docs_client: GoogleDocsClient | None = None,
) -> list[DocumentSyncReport]:
    """設定ファイルを読み込み、全ターゲットの同期を実行する。"""
    targets = load_config(config_path)
    if docs_client is None:
        creds = str(
            credentials_path
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or DEFAULT_CREDENTIALS_PATH
        )
        docs_client = GoogleDocsClient.from_service_account(creds)

    factory = gh_client_factory or (lambda repo: GitHubClient(repo))
    all_reports: list[DocumentSyncReport] = []
    for target in targets:
        gh_client = factory(target.repository)
        reports = sync_target(
            target,
            gh_client,
            docs_client,
            force=force,
            dry_run=dry_run,
            doc_id_filter=doc_id_filter,
            cache_dir=cache_dir,
            output_dir=output_dir,
        )
        all_reports.extend(reports)
    return all_reports


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync GitHub Copilot documentation to Google Docs Tabs.")
    parser.add_argument("--force", action="store_true", help="Run full baseline sync and clean up orphan tabs.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write resolved markdown locally and verify Phase 1 plan/guards without modifying Google Docs.",
    )
    parser.add_argument("--doc-id", default=None, help="Sync only the specified Google Document ID.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.yaml.")
    parser.add_argument("--credentials", default=None, help="Path to GCP service account JSON.")
    parser.add_argument("--cache-dir", default=".", help="Directory for last_commit_sha_*.txt.")
    parser.add_argument("--output-dir", default=DEFAULT_DRY_RUN_DIR, help="Output directory for --dry-run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = build_arg_parser().parse_args(argv)
    try:
        run_sync(
            config_path=args.config,
            credentials_path=args.credentials,
            force=args.force,
            dry_run=args.dry_run,
            doc_id_filter=args.doc_id,
            cache_dir=args.cache_dir,
            output_dir=args.output_dir,
        )
    except Exception as exc:
        logger.error("Sync failed: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

