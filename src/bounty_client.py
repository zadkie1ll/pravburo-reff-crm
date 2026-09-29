from decimal import Decimal

import httpx
from pravburo_ref_common.contracts import RewardCreate
from pravburo_ref_common.models import RewardType

from src.config import get_settings


class BountyClient:
    async def create_reward(
        self,
        *,
        deal_id: str,
        application_id: int,
        agent_id: int,
        reward_type: RewardType = RewardType.MAIN,
        amount: Decimal | None = None,
    ) -> dict[str, object]:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.bounty_service_url.rstrip('/')}/internal/rewards",
                headers={"X-Internal-Token": settings.internal_service_token},
                json=RewardCreate(
                    deal_id=deal_id,
                    application_id=application_id,
                    agent_id=agent_id,
                    reward_type=reward_type,
                    amount=amount,
                ).model_dump(mode="json"),
            )
        response.raise_for_status()
        return response.json()
