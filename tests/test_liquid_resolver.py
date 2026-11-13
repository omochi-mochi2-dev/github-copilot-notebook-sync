"""liquid_resolver.py の単体・結合テスト。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.liquid_resolver import (
    LiquidResolutionError,
    LiquidResolver,
    VariablesDict,
    build_document_markdown,
    normalize_variables,
    split_frontmatter,
)
from tests.conftest import FIXTURES_DIR, make_fixture_loader


def _resolver_with(reusables: dict[str, str], variables: VariablesDict | None = None) -> LiquidResolver:
    return LiquidResolver(lambda p: reusables.get(p), variables or {})


# ---------------------------------------------------------------------------
# Phase A: Reusables
# ---------------------------------------------------------------------------


class TestReusables:
    def test_simple_expansion(self, resolver: LiquidResolver) -> None:
        out = resolver.expand_reusables("Intro: {% data reusables.copilot.about-copilot %}")
        assert out == "Intro: {% data variables.product.prodname_copilot %} is an AI coding assistant."

    def test_dotted_ref_maps_to_path(self) -> None:
        calls: list[str] = []

        def loader(path: str) -> str | None:
            calls.append(path)
            return "X"

        LiquidResolver(loader, {}).expand_reusables("{% data reusables.copilot.mcp.about-github-mcp-server %}")
        assert calls == ["copilot/mcp/about-github-mcp-server"]

    def test_whitespace_control_syntax(self) -> None:
        r = _resolver_with({"a/b": "B"})
        assert r.expand_reusables("{%- data reusables.a.b -%}") == "B"
        assert r.expand_reusables("{%data   reusables.a.b%}") == "B"

    def test_three_levels_expanded_fourth_removed(
        self, resolver: LiquidResolver, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = resolver.expand_reusables("{% data reusables.copilot.nested.level1 %}")
        assert out == "Level 1 start. Level 2. Level 3. "
        assert "Level 4" not in out
        assert "exceeds max depth" in caplog.text

    def test_custom_depth(self) -> None:
        r = LiquidResolver(make_fixture_loader(), {}, max_reusable_depth=1)
        assert r.expand_reusables("{% data reusables.copilot.nested.level1 %}") == "Level 1 start. "

    def test_circular_reference_raises(self, resolver: LiquidResolver) -> None:
        with pytest.raises(LiquidResolutionError, match="Circular"):
            resolver.expand_reusables("{% data reusables.copilot.cycle.a %}")

    def test_self_reference_raises(self) -> None:
        r = _resolver_with({"x/self": "loop {% data reusables.x.self %}"})
        with pytest.raises(LiquidResolutionError):
            r.resolve("{% data reusables.x.self %}")

    def test_same_reusable_twice_is_not_cycle(self) -> None:
        r = _resolver_with({"a/b": "B"})
        assert r.expand_reusables("{% data reusables.a.b %} {% data reusables.a.b %}") == "B B"

    def test_missing_reusable_removed_with_warning(
        self, resolver: LiquidResolver, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = resolver.expand_reusables("A {% data reusables.copilot.nope %} B")
        assert out == "A  B"
        assert "not found" in caplog.text

    def test_loader_errors_propagate(self) -> None:
        def loader(path: str) -> str | None:
            raise ConnectionError("network down")

        with pytest.raises(ConnectionError):
            LiquidResolver(loader, {}).resolve("{% data reusables.a.b %}")

    def test_loader_is_cached(self) -> None:
        calls: list[str] = []

        def loader(path: str) -> str | None:
            calls.append(path)
            return "B"

        r = LiquidResolver(loader, {})
        r.resolve("{% data reusables.a.b %}")
        r.resolve("{% data reusables.a.b %}")
        assert calls == ["a/b"]

    def test_references_inside_comment_are_not_loaded(self) -> None:
        calls: list[str] = []

        def loader(path: str) -> str | None:
            calls.append(path)
            return "X"

        out = LiquidResolver(loader, {}).resolve("A{% comment %}{% data reusables.a.b %}{% endcomment %}B")
        assert calls == []
        assert out == "AB\n"

    def test_indented_data_reference(self) -> None:
        r = _resolver_with({"a/b": "line1\n\nline2"})
        out = r.expand_reusables("1. Step\n{% indented_data_reference reusables.a.b spaces=3 %}")
        assert out == "1. Step\n   line1\n\n   line2"


# ---------------------------------------------------------------------------
# Phase B: UI / control tags
# ---------------------------------------------------------------------------


class TestFlattenUiTags:
    flatten = staticmethod(LiquidResolver.flatten_ui_tags)

    def test_codetabs(self) -> None:
        src = "{% codetabs %}\n{% codetab python %}\ncode\n{% endcodetab %}\n{% endcodetabs %}"
        out = self.flatten(src)
        assert "#### Python" in out and "code" in out
        assert "{%" not in out

    @pytest.mark.parametrize(
        ("lang", "display"),
        [("javascript", "JavaScript"), ("csharp", "C#"), ("typescript", "TypeScript"), ("elixir", "Elixir")],
    )
    def test_codetab_display_names(self, lang: str, display: str) -> None:
        assert self.flatten(f"{{% codetab {lang} %}}") == f"\n\n#### {display}\n\n"

    @pytest.mark.parametrize(
        ("tag", "display"),
        [
            ("vscode", "Visual Studio Code"),
            ("jetbrains", "JetBrains IDEs"),
            ("visualstudio", "Visual Studio"),
            ("xcode", "Xcode"),
            ("eclipse", "Eclipse"),
            ("vimneovim", "Vim/Neovim"),
            ("webui", "Web browser"),
            ("copilotcli", "GitHub Copilot CLI"),
            ("mac", "Mac"),
        ],
    )
    def test_tool_tags_become_h4(self, tag: str, display: str) -> None:
        out = self.flatten(f"{{% {tag} %}}\nBody\n{{% end{tag} %}}")
        assert out == f"\n\n#### {display}\n\n\nBody\n\n"

    def test_ifversion_keeps_all_branches(self) -> None:
        src = "{% ifversion ghes > 3.9 %}A{% elsif ghec %}B{% else %}C{% endif %}"
        assert self.flatten(src) == "ABC"

    def test_if_and_unless(self) -> None:
        assert self.flatten("{% if foo %}X{% endif %}{% unless bar %}Y{% endunless %}") == "XY"

    def test_rowheaders_removed(self) -> None:
        assert self.flatten("{% rowheaders %}\n|a|\n{% endrowheaders %}") == "\n|a|\n"

    def test_octicons(self) -> None:
        assert self.flatten('{% octicon "check" aria-label="Supported" %}') == "✓"
        assert self.flatten("{% octicon 'check' %}") == "✓"
        assert self.flatten('{% octicon "x" aria-label="No" %}') == ""
        assert self.flatten('Click {% octicon "kebab-horizontal" %} menu') == "Click  menu"

    def test_comment_block_removed(self) -> None:
        assert self.flatten("A{% comment %}\nsecret\n{% endcomment %}B") == "AB"

    def test_prompt_tags_keep_content(self) -> None:
        assert self.flatten("{% prompt %}Explain this{% endprompt %}") == "Explain this"

    def test_whitespace_control(self) -> None:
        assert self.flatten("{%- ifversion fpt -%}X{%- endif -%}") == "X"


# ---------------------------------------------------------------------------
# Phase C: Variables
# ---------------------------------------------------------------------------


class TestVariables:
    def test_replace(self, resolver: LiquidResolver) -> None:
        assert resolver.replace_variables("{% data variables.product.prodname_copilot %}") == "GitHub Copilot"
        assert resolver.replace_variables("{% data variables.copilot.copilot_qwen_3.8_flash %}") == "Qwen 3.8 Flash"

    def test_undefined_key_falls_back_to_key_name(
        self, resolver: LiquidResolver, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            assert resolver.replace_variables("{% data variables.copilot.nope_key %}") == "nope_key"
            assert resolver.replace_variables("{% data variables.missingfile.prodname_x %}") == "prodname_x"
        assert "Undefined variable" in caplog.text

    def test_nested_variable_in_value(self, resolver: LiquidResolver) -> None:
        assert resolver.replace_variables("{% data variables.copilot.prodname_copilot_cli %}") == "GitHub Copilot CLI"

    def test_liquid_in_value_is_flattened(self, resolver: LiquidResolver) -> None:
        assert resolver.replace_variables("{% data variables.product.product_url %}") == "HOSTNAMEgithub.com"

    def test_nesting_depth_limit(self, resolver: LiquidResolver) -> None:
        # level1 -> level2 -> level3 -> level4(値 "deep")。4 階層目はキー名にフォールバック
        assert resolver.replace_variables("{% data variables.copilot.level2 %}") == "deep"
        assert resolver.replace_variables("{% data variables.copilot.level1 %}") == "level4"

    def test_cycle_terminates(self, resolver: LiquidResolver) -> None:
        assert "{%" not in resolver.replace_variables("{% data variables.copilot.loop_a %}")

    def test_normalize_variables(self, fixture_variables: VariablesDict) -> None:
        assert fixture_variables["copilot"]["max_premium_requests"] == "300"
        assert "nested_map" not in fixture_variables["copilot"]
        assert normalize_variables({"bad": ["list"], "ok": {"k": True}}) == {"ok": {"k": "True"}}


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_split(self) -> None:
        meta, body = split_frontmatter("---\ntitle: T\nversions:\n  fpt: '*'\n---\n\nBody\n")
        assert meta == {"title": "T", "versions": {"fpt": "*"}}
        assert body == "\nBody\n"

    def test_no_frontmatter(self) -> None:
        assert split_frontmatter("Body only") == ({}, "Body only")

    def test_crlf_frontmatter(self) -> None:
        meta, body = split_frontmatter("---\r\ntitle: T\r\n---\r\nBody")
        assert meta == {"title": "T"} and body == "Body"

    def test_invalid_yaml_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            meta, _ = split_frontmatter("---\ntitle: {% data variables.x.y %} guide\nintro: 'Hi'\n---\nB")
        assert meta["title"] == "{% data variables.x.y %} guide"
        assert meta["intro"] == "Hi"

    def test_build_document_markdown(self) -> None:
        raw = "---\ntitle: My Title\nintro: >-\n  Multi\n  line\nredirect_from:\n  - /old\n---\n\n## Body\n"
        assert build_document_markdown(raw) == "# My Title\n\nMulti line\n\n## Body"

    def test_build_without_intro(self) -> None:
        assert build_document_markdown("---\ntitle: T\n---\nBody") == "# T\n\nBody"

    def test_index_style_frontmatter_only(self) -> None:
        assert build_document_markdown("---\ntitle: Index\nchildren:\n  - /a\n---\n") == "# Index"


# ---------------------------------------------------------------------------
# Pipeline order & integration
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_order_variables_and_ui_tags_inside_reusables(self, resolver: LiquidResolver) -> None:
        out = resolver.resolve("{% data reusables.copilot.ide-tabs %}")
        assert "#### Visual Studio Code" in out
        assert "#### JetBrains IDEs" in out
        assert "Use the  icon in Visual Studio Code." in out
        assert "{%" not in out

    def test_raw_block_preserved_literally(self, resolver: LiquidResolver) -> None:
        out = resolver.resolve("{% data reusables.copilot.raw-snippet %}")
        assert 'echo "${{ github.sha }}" {% data variables.product.prodname_copilot %}' in out
        assert "{% raw %}" not in out and "{% endraw %}" not in out

    def test_unknown_tags_swept_with_warning(
        self, resolver: LiquidResolver, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            out = resolver.resolve("A {% link_in_list /foo %} B {% data ui.header.notice %}", "x.md")
        assert out == "A  B \n"
        assert "unknown Liquid tag" in caplog.text and "link_in_list" in caplog.text

    def test_blank_lines_compressed(self, resolver: LiquidResolver) -> None:
        out = resolver.resolve("A\n\n{% ifversion ghes %}\n\n   \n{% endif %}\n\n\nB")
        assert out == "A\n\nB\n"

    def test_doc1_like_document(self, resolver: LiquidResolver) -> None:
        raw = (FIXTURES_DIR / "content/copilot/concepts/about-copilot.md").read_text(encoding="utf-8")
        out = resolver.resolve_document(raw, "content/copilot/concepts/about-copilot.md")

        # Frontmatter → # title + intro、その他メタデータは除去
        assert out.startswith("# About GitHub Copilot\n\nLearn how Copilot helps you code.\n\n")
        for removed in ("redirect_from", "versions:", "contentType", "shortTitle", "---\n"):
            assert removed not in out
        # Phase A + C
        assert "GitHub Copilot is an AI coding assistant." in out
        assert "1. Open Visual Studio Code.\n1. Sign in to GitHub." in out
        # comment ブロック除去（中の未存在 reusable も読み込まれない）
        assert "TODO" not in out
        # Phase B
        assert "| Chat | ✓ |" in out
        assert "| Agent |  |" in out
        assert "Available on GitHub Enterprise Server." in out and "Available on GitHub." in out
        assert "#### Python" in out and "#### JavaScript" in out
        assert 'print("hello")' in out and 'console.log("hello");' in out
        assert "#### Visual Studio Code" in out and "#### JetBrains IDEs" in out
        assert "Explain this code" in out
        # raw 保護
        assert "${{ github.sha }}" in out
        # 未定義変数フォールバック
        assert "unknown_key fallback." in out
        # Liquid タグが raw ブロック以外に残存しない
        assert out.count("{%") == 1  # raw ブロック内のリテラル 1 件のみ
        assert "\n\n\n" not in out
        assert out.endswith("\n") and not out.endswith("\n\n")

    def test_resolve_is_idempotent_on_plain_markdown(self, resolver: LiquidResolver) -> None:
        plain = "# Title\n\nSome `code` and {curly} braces.\n"
        assert resolver.resolve(plain) == plain

