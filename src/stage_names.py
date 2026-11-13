"""Human stage names from the portal's own dictionaries, cached briefly."""

from time import monotonic

import httpx

from src.bitrix import BitrixError, BitrixGateway

_cache: dict[tuple[str, int], tuple[float, dict[str, str]]] = {}


async def deal_stage_names(gateway: BitrixGateway, category_id: int) -> dict[str, str]:
    key = (gateway.settings.bitrix_webhook_url, category_id)
    cached = _cache.get(key)
    if cached and monotonic() - cached[0] < 300:
        return cached[1]
    entity = "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"
    try:
        rows = await gateway._call("crm.status.list", {"filter": {"ENTITY_ID": entity}})
        names = {
            str(row["STATUS_ID"]): str(row["NAME"]).strip()
            for row in rows
            if str(row.get("NAME") or "").strip()
        }
    except (BitrixError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        return cached[1] if cached else {}
    _cache[key] = (monotonic(), names)
    return names
