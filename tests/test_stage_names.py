import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import stage_names
from src.bitrix import BitrixError


def gateway(url, result):
    return SimpleNamespace(
        settings=SimpleNamespace(bitrix_webhook_url=url),
        _call=AsyncMock(return_value=result),
    )


def test_stage_dictionary_is_reused_inside_cache_window():
    portal = gateway("https://one.test/rest", [{"STATUS_ID": "WON", "NAME": "Сделка успешна"}])

    async def run():
        assert await stage_names.deal_stage_names(portal, 0) == {"WON": "Сделка успешна"}
        assert await stage_names.deal_stage_names(portal, 0) == {"WON": "Сделка успешна"}

    asyncio.run(run())
    portal._call.assert_awaited_once_with(
        "crm.status.list", {"filter": {"ENTITY_ID": "DEAL_STAGE"}}
    )


@pytest.mark.parametrize("malformed", [False, True])
def test_expired_dictionary_keeps_human_names_when_refresh_fails(monkeypatch, malformed):
    now = [0]
    monkeypatch.setattr(stage_names, "monotonic", lambda: now[0])
    portal = gateway("https://one.test/rest", [{"STATUS_ID": "WON", "NAME": "Сделка успешна"}])

    async def run():
        expected = await stage_names.deal_stage_names(portal, 0)
        now[0] = 301
        if malformed:
            portal._call.return_value = None
        else:
            portal._call.side_effect = BitrixError("Unavailable")
        assert await stage_names.deal_stage_names(portal, 0) == expected

    asyncio.run(run())
    assert portal._call.await_count == 2


def test_portals_and_funnels_do_not_share_dictionary_entries():
    first = gateway("https://one.test/rest", [{"STATUS_ID": "NEW", "NAME": "Начало продаж"}])
    second = gateway("https://two.test/rest", [{"STATUS_ID": "NEW", "NAME": "Другая воронка"}])

    async def run():
        assert (await stage_names.deal_stage_names(first, 0))["NEW"] == "Начало продаж"
        first._call.return_value = [{"STATUS_ID": "C2:NEW", "NAME": "Сопровождение"}]
        assert (await stage_names.deal_stage_names(first, 2))["C2:NEW"] == "Сопровождение"
        assert (await stage_names.deal_stage_names(second, 0))["NEW"] == "Другая воронка"
        assert (await stage_names.deal_stage_names(first, 0))["NEW"] == "Начало продаж"

    asyncio.run(run())
    assert first._call.await_count == 2
    assert second._call.await_count == 1
