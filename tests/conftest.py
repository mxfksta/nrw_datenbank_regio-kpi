"""Gemeinsame Test-Fixtures. Tests greifen NIE auf das Netz zu:
HTTP wird mit `responses` gemockt; unregistrierte URLs schlagen fehl."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.net as net
from src.config import Region, Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def region_lev() -> Region:
    return Region(
        name="Leverkusen",
        regionalschluessel="05316",
        typ="Kreisfreie Stadt",
        regierungsbezirk="Köln",
    )


@pytest.fixture
def settings() -> Settings:
    # rate_limit 0 → keine künstlichen Pausen in Tests
    return Settings(rate_limit_seconds=0.0, http_timeout_seconds=5.0)


@pytest.fixture(autouse=True)
def _reset_net_caches():
    net.reset_caches()
    yield
    net.reset_caches()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
