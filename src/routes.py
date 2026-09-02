import hmac
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pravburo_ref_common.contracts import LeadCreate

from src.bitrix import BitrixGateway, LeadData, extract_attribution_marker
from src.bounty_client import BountyClient
from src.config import get_settings
from src.internal_auth import require_internal_token

router = APIRouter(tags=["CRM"])
logger = logging.getLogger(__name__)


def extract_bitrix_deal_id(payload: dict) -> str | None:
    """Read both our JSON payload and Bitrix's document_id[2]=DEAL_<id> format."""
    direct_id = payload.get("deal_id") or payload.get("data[FIELDS][ID]")
    if direct_id:
        return str(direct_id).strip()
    nested_data = payload.get("data")
    if isinstance(nested_data, dict):
        fields = nested_data.get("FIELDS")
        if isinstance(fields, dict) and fields.get("ID"):
            return str(fields["ID"]).strip()
    match = re.search(r"DEAL_(\d+)", str(payload.get("document_id[2]") or ""))
    return match.group(1) if match else None


@router.post("/internal/leads", dependencies=[Depends(require_internal_token)])
async def create_lead(payload: LeadCreate) -> dict[str, str]:
    lead_id = await BitrixGateway().create_lead(LeadData(**payload.model_dump()))
    logger.info(
        "Bitrix lead created: application_id=%s agent_id=%s lead_id=%s",
        payload.application_id,
        payload.agent_id,
        lead_id,
    )
    return {"status": "created", "lead_id": lead_id}


@router.post("/webhooks/bitrix/deal-category")
async def deal_category_webhook(
    request: Request,
    webhook_secret: str = Header(default="", alias="X-Webhook-Secret"),
) -> dict[str, object]:
    settings = get_settings()
    if not settings.bitrix_webhook_secret or not hmac.compare_digest(
        webhook_secret, settings.bitrix_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    content_type = request.headers.get("content-type", "")
    payload = (
        await request.json() if "application/json" in content_type else dict(await request.form())
    )
    deal_id = extract_bitrix_deal_id(payload)
    if not deal_id:
        raise HTTPException(
            status_code=400,
            detail="Deal ID is required (expected document_id[2]=DEAL_<id>)",
        )
    deal = await BitrixGateway().get_deal(deal_id)
    if int(deal.get("CATEGORY_ID", -1)) != settings.bitrix_client_category_id:
        return {"status": "ignored", "reason": "category"}
    marker = extract_attribution_marker(deal.get("SOURCE_DESCRIPTION"))
    if marker is None:
        return {"status": "ignored", "reason": "no_agent_attribution"}
    agent_id, application_id = marker
    return await BountyClient().create_reward(
        deal_id=deal_id,
        application_id=application_id,
        agent_id=agent_id,
    )


@router.get(
    "/internal/deals/{deal_id}/contact-phone",
    dependencies=[Depends(require_internal_token)],
)
async def deal_contact_phone(deal_id: str) -> dict[str, str | None]:
    return {"phone": await BitrixGateway().get_deal_contact_phone(deal_id)}
