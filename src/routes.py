import asyncio
import hmac
import logging
import re
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pravburo_ref_common.contracts import LeadCreate
from pravburo_ref_common.models import RewardType

from src.bitrix import (
    BitrixError,
    BitrixGateway,
    BitrixNotFound,
    LeadData,
    extract_attribution_marker,
    first_payment_rubles,
    is_paid_in_full,
)
from src.bounty_client import BountyClient
from src.client_search import ClientSearch, name_tokens
from src.config import get_settings
from src.internal_auth import require_internal_token
from src.site_client import SiteClient, SiteLookupError
from src.stage_names import deal_stage_names

router = APIRouter(tags=["CRM"])
logger = logging.getLogger(__name__)

# Фикс от руководителя: бонус за то, что клиент оплатил 100% суммы (OPPORTUNITY)
# первым платежом, а не в рассрочку - не зависит от уровня партнёра.
BONUS_FULL_PAYMENT_AMOUNT = Decimal("3000")

# Стадии сделок из всех принимаемых воронок (BITRIX_CLIENT_CATEGORY_IDS) уходят на сайт,
# а выплаты создаются только для воронки "Сопровождение" (category_id=2).
REWARD_CATEGORY_ID = 2

# Воронка "Сопровождение" (category_id=2). Аванс партнёру полагается после
# первого платежа от 8000 ₽ на любой стадии; повторный вызов bounty не дублирует.
# основная выплата - когда сделка дошла до стадии MAIN_REWARD_STAGE.
# Суммы настраиваются в bounty на /admin/reward-rates.
# TODO: стадию для основной выплаты уточнить у руководителя ("Депозит оплачен" по ТЗ).
MAIN_REWARD_STAGE = "C2:UC_MYA2I0"  # "ОБСУЖДЕНИЕ депозита"
MIN_FIRST_PAYMENT_FOR_ADVANCE = Decimal("8000")


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
    category_id = int(deal.get("CATEGORY_ID", -1))
    if category_id not in settings.client_category_ids:
        return {"status": "ignored", "reason": "category"}
    marker = extract_attribution_marker(deal.get("SOURCE_DESCRIPTION"))
    if marker is None:
        try:
            binding = await SiteClient().resolve_existing_deal(deal_id)
        except SiteLookupError:
            raise HTTPException(
                status_code=502, detail="Existing deal lookup unavailable"
            ) from None
        if binding is None:
            return {"status": "ignored", "reason": "no_agent_attribution"}
        agent_id, application_id = binding["agent_id"], binding["application_id"]
    else:
        # Preserve marker flows during site outages. Imported deals with stale
        # markers are rejected by the site's/bounty's attribution guards.
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
    if category_id != REWARD_CATEGORY_ID:
        # До договора (воронка "Основная") клиент только меняет этап: выплаты ещё нет.
        return {"status": "stage_synced"}

    first_payment = first_payment_rubles(deal)
    reward_types = []
    if first_payment is not None and first_payment >= MIN_FIRST_PAYMENT_FOR_ADVANCE:
        reward_types.append(RewardType.ADVANCE)
    if str(stage_code) == MAIN_REWARD_STAGE:
        reward_types.append(RewardType.MAIN)
        if is_paid_in_full(deal):
            reward_types.append(RewardType.BONUS_FULL_PAYMENT)

    if not reward_types:
        return {"status": "stage_synced"}

    rewards = [
        await BountyClient().create_reward(
            deal_id=deal_id,
            application_id=application_id,
            agent_id=agent_id,
            reward_type=reward_type,
            amount=(
                BONUS_FULL_PAYMENT_AMOUNT if reward_type == RewardType.BONUS_FULL_PAYMENT else None
            ),
        )
        for reward_type in reward_types
    ]
    return {"status": "processed", "rewards": rewards}


@router.get("/internal/clients/search", dependencies=[Depends(require_internal_token)])
async def search_clients(
    q: str = Query(min_length=3, max_length=200),
    page: int = Query(default=1, ge=1, le=1000),
) -> dict[str, object]:
    try:
        tokens = name_tokens(q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        async with asyncio.timeout(30):
            return await ClientSearch(BitrixGateway()).search(tokens, page)
    except (
        BitrixError,
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
    ):
        raise HTTPException(status_code=502, detail="Bitrix lookup unavailable") from None


@router.get(
    "/internal/deals/{deal_id}/existing-client",
    dependencies=[Depends(require_internal_token)],
)
async def existing_client(
    deal_id: str = Path(min_length=1, max_length=64, pattern=r"^[0-9]+$"),
) -> dict[str, object]:
    if int(deal_id) <= 0:
        raise HTTPException(status_code=422, detail="Deal ID must be positive")
    gateway = BitrixGateway()
    try:
        deal = await gateway.get_deal(deal_id)
        if not deal:
            raise HTTPException(status_code=404, detail="Deal not found")
        category_id = int(deal.get("CATEGORY_ID", -1))
        if category_id not in get_settings().client_category_ids:
            raise HTTPException(status_code=404, detail="Deal not found")
        contact_id = deal.get("CONTACT_ID")
        contact = await gateway.get_contact(str(contact_id)) if contact_id else {}
        full_name = (
            " ".join(
                str(contact.get(field) or "").strip()
                for field in ("LAST_NAME", "NAME", "SECOND_NAME")
                if str(contact.get(field) or "").strip()
            )
            or None
        )
        phones = contact.get("PHONE") or []
        phone = next(
            (str(item["VALUE"]).strip() for item in phones if str(item.get("VALUE") or "").strip()),
            None,
        )
        marker = extract_attribution_marker(deal.get("SOURCE_DESCRIPTION"))
        first_payment = first_payment_rubles(deal)
        names = await deal_stage_names(gateway, category_id)
        return {
            "deal_id": deal_id,
            "full_name": full_name,
            "phone": phone,
            "stage_code": str(deal.get("STAGE_ID") or ""),
            "stage_label": names.get(str(deal.get("STAGE_ID") or ""), "Название этапа недоступно"),
            "category_id": category_id,
            "first_payment": str(first_payment) if first_payment is not None else None,
            "paid_in_full": is_paid_in_full(deal),
            "attributed_agent_id": marker[0] if marker else None,
            "attributed_application_id": marker[1] if marker else None,
        }
    except BitrixNotFound:
        raise HTTPException(status_code=404, detail="Deal not found") from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Deal not found") from None
        raise HTTPException(status_code=502, detail="Bitrix lookup unavailable") from None
    except (BitrixError, httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(status_code=502, detail="Bitrix lookup unavailable") from None


@router.get(
    "/internal/deals/{deal_id}/contact-phone",
    dependencies=[Depends(require_internal_token)],
)
async def deal_contact_phone(deal_id: str) -> dict[str, str | None]:
    return {"phone": await BitrixGateway().get_deal_contact_phone(deal_id)}


@router.get("/internal/deal-stage-labels", dependencies=[Depends(require_internal_token)])
async def deal_stage_labels() -> dict[str, str]:
    gateway = BitrixGateway()
    names = {}
    for category_id in sorted(get_settings().client_category_ids):
        names.update(await deal_stage_names(gateway, category_id))
    return names
