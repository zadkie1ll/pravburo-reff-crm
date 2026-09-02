import re
from dataclasses import dataclass
from typing import Any

import httpx

from src.config import get_settings

MARKER_RE = re.compile(r"\[pravburo-agent:v1;agent_id=(\d+);application_id=(\d+)\]")


class BitrixError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LeadData:
    application_id: int
    agent_id: int
    agent_name: str
    full_name: str
    phone_normalized: str
    preferred_call_time_msk: str | None = None
    city: str | None = None
    debt_amount: str | None = None
    situation: str | None = None


class BitrixGateway:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if not self.settings.bitrix_webhook_url:
            raise BitrixError("Bitrix API is not configured")
        url = f"{self.settings.bitrix_webhook_url.rstrip('/')}/{method}.json"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise BitrixError(data.get("error_description") or data["error"])
        return data.get("result")

    async def create_lead(self, lead: LeadData) -> str:
        marker = (
            f"[pravburo-agent:v1;agent_id={lead.agent_id};application_id={lead.application_id}]"
        )
        details = [
            f"Желаемое время звонка (МСК): {lead.preferred_call_time_msk or '-'}",
            f"Город: {lead.city or '-'}",
            f"Сумма долга: {lead.debt_amount or '-'}",
            f"Ситуация: {lead.situation or '-'}",
        ]
        result = await self._call(
            "crm.lead.add",
            {
                "fields": {
                    "TITLE": f"Реферальная заявка: {lead.full_name}",
                    "NAME": lead.full_name,
                    "PHONE": [{"VALUE": lead.phone_normalized, "VALUE_TYPE": "WORK"}],
                    "SOURCE_ID": self.settings.bitrix_agent_source_id,
                    "SOURCE_DESCRIPTION": f"Агент: {lead.agent_name or lead.agent_id}\n{marker}",
                    "COMMENTS": "\n".join(details),
                }
            },
        )
        if result is None or str(result).strip() == "":
            raise BitrixError("Bitrix did not return the created lead ID")
        return str(result)

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        result = await self._call("crm.deal.get", {"id": deal_id})
        return result or {}

    async def get_deal_contact_phone(self, deal_id: str) -> str | None:
        deal = await self.get_deal(deal_id)
        contact_id = deal.get("CONTACT_ID")
        if not contact_id:
            return None
        contact = await self._call("crm.contact.get", {"id": contact_id}) or {}
        phones = contact.get("PHONE") or []
        return str(phones[0].get("VALUE")) if phones and phones[0].get("VALUE") else None


def extract_attribution_marker(source_description: str | None) -> tuple[int, int] | None:
    match = MARKER_RE.search(source_description or "")
    return (int(match.group(1)), int(match.group(2))) if match else None
