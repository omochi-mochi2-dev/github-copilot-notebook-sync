"""Liquid 構文 3 段階解決エンジン（docs/Detailed_Logic.md §4 準拠）。

Raw Markdown
  ▼ Phase A: Reusables 展開（data/reusables/ のスニペットを最大 3 階層まで再帰展開）
  ▼ Phase B: UI・制御タグ平坦化（codetab / IDE タグを H4 見出し化、ifversion 等を除去）
  ▼ Phase C: Variables 辞書置換（data/variables/*.yml）
Clean & Flattened Markdown

データ取得（GitHub 通信）からは完全に切り離されており、Reusables は `ReusableLoader`
コールバック、Variables は辞書として注入する。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from src.models import MAX_REUSABLE_DEPTH

logger = logging.getLogger(__name__)

#: reusable のドット区切り識別子（例: "copilot/mcp/about-github-mcp-server"）から Markdown 本文を返す。
#: 存在しない場合は None を返す。通信エラー等は例外として送出すること（サイレントな本文欠落を防ぐ）。
ReusableLoader = Callable[[str], str | None]

#: {"product": {"prodname_copilot": "GitHub Copilot"}, ...}
VariablesDict = dict[str, dict[str, str]]


class LiquidResolutionError(RuntimeError):
    """循環参照など、解決を継続できない Liquid 構文エラー。"""


# ---------------------------------------------------------------------------
# 正規表現定義
# ---------------------------------------------------------------------------

# `{%` / `{%-` と `%}` / `-%}` の両記法に対応する
_OPEN = r"\{%-?\s*"
_CLOSE = r"\s*-?%\}"

RAW_BLOCK_RE = re.compile(_OPEN + r"raw" + _CLOSE + r"([\s\S]*?)" + _OPEN + r"endraw" + _CLOSE)
COMMENT_BLOCK_RE = re.compile(_OPEN + r"comment" + _CLOSE + r"[\s\S]*?" + _OPEN + r"endcomment" + _CLOSE)

# Phase A
REUSABLE_RE = re.compile(_OPEN + r"data\s+reusables\.([^\s%]+)" + _CLOSE)
INDENTED_REUSABLE_RE = re.compile(
    _OPEN + r"indented_data_reference\s+reusables\.([^\s%]+)(?:\s+spaces=(\d+))?" + _CLOSE
)

# Phase B
CODETABS_RE = re.compile(_OPEN + r"(?:codetabs|endcodetabs)" + _CLOSE)
CODETAB_RE = re.compile(_OPEN + r"codetab\s+([\w#+.-]+)" + _CLOSE)
ENDCODETAB_RE = re.compile(_OPEN + r"endcodetab" + _CLOSE)
CONDITIONAL_RE = re.compile(
    _OPEN + r"(?:ifversion|elsif|else|endif|if|unless|endunless)\b(?:(?!%\}).)*?-?%\}"
)
DECORATION_RE = re.compile(
    _OPEN + r"(?:rowheaders|endrowheaders|prompt|endprompt|note|endnote|tip|endtip"
    r"|warning|endwarning|caution|endcaution|danger|enddanger|important|endimportant)" + _CLOSE
)
OCTICON_RE = re.compile(_OPEN + r"octicon\s+[\"']([^\"']+)[\"'](?:(?!%\}).)*?-?%\}")

# Phase C（YAML キー名に `3.8` 等のドットが含まれるケースにも対応）
VARIABLE_RE = re.compile(_OPEN + r"data\s+variables\.([\w-]+)\.([\w.-]+)" + _CLOSE)

# 最終スイープ（未知の残存タグ）
ANY_TAG_RE = re.compile(r"\{%-?[\s\S]*?-?%\}")
TAG_NAME_RE = re.compile(r"\{%-?\s*([\w-]+)")

FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|\Z)")

# ---------------------------------------------------------------------------
# 表示名辞書
# ---------------------------------------------------------------------------

LANG_DISPLAY_NAMES: dict[str, str] = {
    "javascript": "JavaScript",
    "js": "JavaScript",
    "typescript": "TypeScript",
    "ts": "TypeScript",
    "python": "Python",
    "csharp": "C#",
    "cs": "C#",
    "dotnet": ".NET",
    "go": "Go",
    "java": "Java",
    "ruby": "Ruby",
    "shell": "Shell",
    "bash": "Bash",
    "powershell": "PowerShell",
    "yaml": "YAML",
    "json": "JSON",
    "cpp": "C++",
    "kotlin": "Kotlin",
    "swift": "Swift",
    "rust": "Rust",
    "php": "PHP",
    "curl": "cURL",
    "cli": "CLI",
}

#: IDE / プラットフォーム / ツール切り替えタグ → H4 見出しテキスト
TOOL_DISPLAY_NAMES: dict[str, str] = {
    "vscode": "Visual Studio Code",
    "visualstudio": "Visual Studio",
    "jetbrains": "JetBrains IDEs",
    "xcode": "Xcode",
    "eclipse": "Eclipse",
    "vimneovim": "Vim/Neovim",
    "azure_data_studio": "Azure Data Studio",
    "webui": "Web browser",
    "cli": "GitHub CLI",
    "desktop": "GitHub Desktop",
    "curl": "cURL",
    "codespaces": "Codespaces",
    "mac": "Mac",
    "windows": "Windows",
    "linux": "Linux",
}

_TOOL_ALTERNATION = "|".join(sorted((re.escape(k) for k in TOOL_DISPLAY_NAMES), key=len, reverse=True))
TOOL_OPEN_RE = re.compile(_OPEN + r"(" + _TOOL_ALTERNATION + r")" + _CLOSE)
TOOL_CLOSE_RE = re.compile(_OPEN + r"end(?:" + _TOOL_ALTERNATION + r")" + _CLOSE)

_RAW_PLACEHOLDER = "\x00LIQUIDRAW{0}\x00"
_RAW_PLACEHOLDER_RE = re.compile("\x00LIQUIDRAW(\\d+)\x00")


def _lang_display_name(lang: str) -> str:
    return LANG_DISPLAY_NAMES.get(lang.lower(), lang[:1].upper() + lang[1:])


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """YAML Frontmatter を分離し (meta, body) を返す。Frontmatter がなければ ({}, text)。"""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta = match.group(1)
    body = text[match.end() :]
    try:
        meta = yaml.safe_load(raw_meta)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse frontmatter as YAML (%s); falling back to line-based parsing.", exc)
        meta = _parse_frontmatter_fallback(raw_meta)
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


def _parse_frontmatter_fallback(raw_meta: str) -> dict[str, Any]:
    """YAML として解釈できない Frontmatter から title / intro のみを行単位で抽出する。"""
    result: dict[str, Any] = {}
    for line in raw_meta.splitlines():
        m = re.match(r"^(title|intro):\s*(.+?)\s*$", line)
        if m:
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            result[m.group(1)] = value
    return result


def build_document_markdown(raw_markdown: str) -> str:
    """Frontmatter を除去し、本文先頭に `# title` と intro を配置した Markdown を返す。"""
    meta, body = split_frontmatter(raw_markdown)
    parts: list[str] = []
    title = meta.get("title")
    intro = meta.get("intro")
    if isinstance(title, str) and title.strip():
        parts.append(f"# {title.strip()}")
    if isinstance(intro, str) and intro.strip():
        parts.append(intro.strip())
    stripped_body = body.strip("\r\n")
    if stripped_body:
        parts.append(stripped_body)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


def normalize_variables(raw_files: dict[str, Any]) -> VariablesDict:
    """
    {ファイル名(拡張子なし): YAML ロード結果} をフラットな Dict[str, Dict[str, str]] に正規化する。
    数値・真偽値は文字列化し、ネストした辞書やリストなどの非スカラー値は除外する。
    """
    normalized: VariablesDict = {}
    for file_key, content in raw_files.items():
        if not isinstance(content, dict):
            logger.debug("Variables file %r is not a mapping; skipped.", file_key)
            continue
        flat: dict[str, str] = {}
        for key, value in content.items():
            if isinstance(value, str):
                flat[str(key)] = value
            elif isinstance(value, (int, float, bool)):
                flat[str(key)] = str(value)
            else:
                logger.debug("Variable %s.%s has non-scalar value; skipped.", file_key, key)
        normalized[file_key] = flat
    return normalized


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@dataclass
class _ResolveContext:
    """1 回の resolve 呼び出しに閉じた状態（raw ブロック退避領域など）。"""

    raw_blocks: list[str] = field(default_factory=list)

    def protect(self, text: str) -> str:
        def _store(match: re.Match[str]) -> str:
            # ブロック記法（タグ直後/直前の改行）による空行の重複を避けるため、前後 1 改行のみ除去する
            content = match.group(1)
            content = re.sub(r"\A\r?\n", "", content)
            content = re.sub(r"\r?\n\Z", "", content)
            self.raw_blocks.append(content)
            return _RAW_PLACEHOLDER.format(len(self.raw_blocks) - 1)

        return RAW_BLOCK_RE.sub(_store, text)

    def restore(self, text: str) -> str:
        return _RAW_PLACEHOLDER_RE.sub(lambda m: self.raw_blocks[int(m.group(1))], text)


class LiquidResolver:
    """GitHub Docs の Liquid 構文を Gemini Notebook 向けのプレーンな Markdown に解決する。"""

    def __init__(
        self,
        reusable_loader: ReusableLoader,
        variables: VariablesDict,
        max_reusable_depth: int = MAX_REUSABLE_DEPTH,
    ) -> None:
        self._loader = reusable_loader
        self._variables = variables
        self._max_depth = max_reusable_depth
        self._reusable_cache: dict[str, str | None] = {}
        self._variable_cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ public

    def resolve_document(self, raw_markdown: str, source_path: str = "") -> str:
        """Frontmatter 処理（# title + intro）を行った上で 3 段階解決を実行する。"""
        return self.resolve(build_document_markdown(raw_markdown), source_path)

    def resolve(self, raw_markdown: str, source_path: str = "") -> str:
        """3 段階解決パイプライン（A → B → C）を順序どおり実行する。"""
        ctx = _ResolveContext()
        text = ctx.protect(raw_markdown)
        text = self._expand_reusables(text, ctx, depth=1, stack=(), source_path=source_path)  # Phase A
        text = self.flatten_ui_tags(text)  # Phase B
        text = self.replace_variables(text)  # Phase C
        text = self._sweep_unknown_tags(text, source_path)
        text = _normalize_whitespace(text)
        return ctx.restore(text)

    def expand_reusables(self, text: str, source_path: str = "") -> str:
        """Phase A のみを実行する（テスト・デバッグ用）。"""
        ctx = _ResolveContext()
        return ctx.restore(self._expand_reusables(ctx.protect(text), ctx, 1, (), source_path))

    @staticmethod
    def flatten_ui_tags(text: str) -> str:
        """Phase B: UI・制御タグ平坦化。"""
        text = COMMENT_BLOCK_RE.sub("", text)
        text = CODETABS_RE.sub("", text)
        text = CODETAB_RE.sub(lambda m: f"\n\n#### {_lang_display_name(m.group(1))}\n\n", text)
        text = ENDCODETAB_RE.sub("\n", text)
        text = TOOL_OPEN_RE.sub(lambda m: f"\n\n#### {TOOL_DISPLAY_NAMES[m.group(1)]}\n\n", text)
        text = TOOL_CLOSE_RE.sub("\n", text)
        text = CONDITIONAL_RE.sub("", text)
        text = DECORATION_RE.sub("", text)
        text = OCTICON_RE.sub(lambda m: "✓" if m.group(1) == "check" else "", text)
        return text

    def replace_variables(self, text: str) -> str:
        """Phase C: Variables 辞書置換。"""
        return VARIABLE_RE.sub(lambda m: self._resolve_variable(m.group(1), m.group(2), depth=1), text)

    # ----------------------------------------------------------------- Phase A

    def _load_reusable(self, ref: str) -> str | None:
        if ref not in self._reusable_cache:
            rel_path = ref.replace(".", "/")
            self._reusable_cache[ref] = self._loader(rel_path)
        return self._reusable_cache[ref]

    def _expand_reusables(
        self,
        text: str,
        ctx: _ResolveContext,
        depth: int,
        stack: tuple[str, ...],
        source_path: str,
    ) -> str:
        # コメントブロック内の参照は展開しない（Phase B の除去規則を先行適用。結果は同一）
        text = COMMENT_BLOCK_RE.sub("", text)

        def _expand(ref: str) -> str | None:
            if ref in stack:
                chain = " -> ".join((*stack, ref))
                raise LiquidResolutionError(f"Circular reusable reference detected in {source_path!r}: {chain}")
            if depth > self._max_depth:
                logger.warning(
                    "Reusable %r exceeds max depth %d in %r (chain: %s); tag removed.",
                    ref, self._max_depth, source_path, " -> ".join(stack),
                )
                return None
            content = self._load_reusable(ref)
            if content is None:
                logger.warning("Reusable %r not found (referenced from %r); tag removed.", ref, source_path)
                return None
            content = ctx.protect(content).rstrip()
            return self._expand_reusables(content, ctx, depth + 1, (*stack, ref), source_path)

        def _sub_plain(match: re.Match[str]) -> str:
            return _expand(match.group(1)) or ""

        def _sub_indented(match: re.Match[str]) -> str:
            expanded = _expand(match.group(1))
            if not expanded:
                return ""
            indent = " " * int(match.group(2) or 0)
            return "\n".join(indent + line if line.strip() else line for line in expanded.split("\n"))

        text = INDENTED_REUSABLE_RE.sub(_sub_indented, text)
        return REUSABLE_RE.sub(_sub_plain, text)

    # ----------------------------------------------------------------- Phase C

    def _resolve_variable(self, file_key: str, key: str, depth: int) -> str:
        # キャッシュは最上位（depth == 1）の完全解決結果のみを対象とする。
        # ネスト途中でキャッシュを参照すると、深さ制限の判定が呼び出し順に依存してしまうため。
        cache_key = (file_key, key)
        if depth == 1 and cache_key in self._variable_cache:
            return self._variable_cache[cache_key]

        value = self._variables.get(file_key, {}).get(key)
        if value is None:
            logger.warning("Undefined variable %s.%s; falling back to key name.", file_key, key)
            return key
        if depth > self._max_depth:
            logger.warning("Variable %s.%s exceeds max nesting depth; falling back to key name.", file_key, key)
            return key

        # 変数値自体に含まれる Liquid（ifversion / 他変数参照）を解決する
        resolved = self.flatten_ui_tags(value)
        resolved = VARIABLE_RE.sub(lambda m: self._resolve_variable(m.group(1), m.group(2), depth + 1), resolved)
        resolved = ANY_TAG_RE.sub("", resolved).strip()
        if depth == 1:
            self._variable_cache[cache_key] = resolved
        return resolved

    # ------------------------------------------------------------- final sweep

    @staticmethod
    def _sweep_unknown_tags(text: str, source_path: str) -> str:
        """3 段階処理後も残存する未知の Liquid タグを除去し、種類ごとに警告する。"""
        leftovers = ANY_TAG_RE.findall(text)
        if not leftovers:
            return text
        names: Counter[str] = Counter()
        for tag in leftovers:
            name_match = TAG_NAME_RE.match(tag)
            names[name_match.group(1) if name_match else "?"] += 1
        logger.warning("Removed %d unknown Liquid tag(s) in %r: %s", len(leftovers), source_path, dict(names))
        return ANY_TAG_RE.sub("", text)


def _normalize_whitespace(text: str) -> str:
    """タグ除去で生じた空白行を整理する（空白のみの行を空行化し、3 行以上の連続空行を 2 行に圧縮）。"""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(?m)^[ \t]+$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"
