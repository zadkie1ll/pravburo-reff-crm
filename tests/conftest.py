"""Keep portal dictionary caches isolated between test providers."""

import pytest

from src.stage_names import _cache


@pytest.fixture(autouse=True)
def isolated_stage_names():
    _cache.clear()
    yield
    _cache.clear()
