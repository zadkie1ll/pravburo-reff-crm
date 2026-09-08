import httpx
from pravburo_ref_common.contracts import DealStageUpdate

from src.config import get_settings


class SiteClient:
    async def update_deal_stage(self, *, application_id: int, deal_id: str, stage_code: str) -> None:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{settings.site_service_url.rstrip('/')}/internal/applications/{application_id}/stage",
                headers={"X-Internal-Token": settings.internal_service_token},
                json=DealStageUpdate(
                    application_id=application_id, deal_id=deal_id, stage_code=stage_code
                ).model_dump(mode="json"),
            )
        response.raise_for_status()
