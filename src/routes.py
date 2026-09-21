import hmac
import logging
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pravburo_ref_common.contracts import LeadCreate
from pravburo_ref_common.models import RewardType

from src.bitrix import BitrixGateway, LeadData, extract_attribution_marker
from src.bounty_client import BountyClient
from src.config import get_settings
from src.internal_auth import require_internal_token
from src.site_client import SiteClient

router = APIRouter(tags=["CRM"])
logger = logging.getLogger(__name__)

# Воронка "Сопровождение" (category_id=2). Аванс партнёру полагается, как только
# сделка попала в воронку (на любой её стадии; повторный вызов bounty не дублирует),
# основная выплата - когда сделка дошла до стадии MAIN_REWARD_STAGE.
# Суммы настраиваются в bounty на /admin/reward-rates.
# TODO: стадию для основной выплаты уточнить у руководителя ("Депозит оплачен" по ТЗ).
MAIN_REWARD_STAGE = "C2:UC_MYA2I0"  # "ОБСУЖДЕНИЕ депозита"


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
    # Bitrix's built-in "Исходящий вебхук" automation robot only lets you
    # configure a target URL, no custom headers - so it passes the secret
    # as a query param instead of X-Webhook-Secret.
    provided_secret = webhook_secret or request.query_params.get("secret", "")
    if not settings.bitrix_webhook_secret or not hmac.compare_digest(
        provided_secret, settings.bitrix_webhook_secret
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

    stage_code = deal.get("STAGE_ID")
    if stage_code:
        try:
            await SiteClient().update_deal_stage(
                application_id=application_id, deal_id=deal_id, stage_code=str(stage_code)
            )
        except Exception:
            logger.warning(
                "Failed to sync deal stage to site: deal_id=%s application_id=%s",
                deal_id,
                application_id,
            )

    if not stage_code:
        return {"status": "ignored", "reason": "no_stage"}

    reward_types = [RewardType.ADVANCE]
    if str(stage_code) == MAIN_REWARD_STAGE:
        reward_types.append(RewardType.MAIN)

    rewards = [
        await BountyClient().create_reward(
            deal_id=deal_id,
            application_id=application_id,
            agent_id=agent_id,
            reward_type=reward_type,
        )
        for reward_type in reward_types
    ]
    return {"status": "processed", "rewards": rewards}


@router.get(
    "/internal/deals/{deal_id}/contact-phone",
    dependencies=[Depends(require_internal_token)],
)
async def deal_contact_phone(deal_id: str) -> dict[str, str | None]:
    return {"phone": await BitrixGateway().get_deal_contact_phone(deal_id)}
