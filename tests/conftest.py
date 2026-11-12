"""共通フィクスチャ: tests/fixtures/data を github/docs の data/ 相当として扱う。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.liquid_resolver import LiquidResolver, ReusableLoader, VariablesDict, normalize_variables

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DATA_DIR = FIXTURES_DIR / "data"


def make_fixture_loader(base: Path = DATA_DIR / "reusables") -> ReusableLoader:
    def _load(rel_path: str) -> str | None:
        path = base / f"{rel_path}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    return _load


def load_fixture_variables(base: Path = DATA_DIR / "variables") -> VariablesDict:
    raw = {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in base.glob("*.yml")}
    return normalize_variables(raw)


@pytest.fixture
def fixture_variables() -> VariablesDict:
    return load_fixture_variables()


@pytest.fixture
def resolver(fixture_variables: VariablesDict) -> LiquidResolver:
    return LiquidResolver(make_fixture_loader(), fixture_variables)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent

