import httpx
from pravburo_ref_common.contracts import DealStageUpdate
from pydantic import BaseModel, Field

from src.config import get_settings


class ExistingDealBinding(BaseModel):
    agent_id: int = Field(strict=True, gt=0)
    application_id: int = Field(strict=True, gt=0)


class SiteLookupError(RuntimeError):
    pass


class SiteClient:
    async def resolve_existing_deal(self, deal_id: str) -> dict[str, int] | None:
        settings = get_settings()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{settings.site_service_url.rstrip('/')}"
                    f"/internal/existing-clients/deals/{deal_id}",
                    headers={"X-Internal-Token": settings.internal_service_token},
                )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            binding = ExistingDealBinding.model_validate(response.json())
            return {"agent_id": binding.agent_id, "application_id": binding.application_id}
        except (httpx.HTTPError, ValueError) as exc:
            raise SiteLookupError("Existing deal lookup failed") from exc

    async def update_deal_stage(
        self, *, application_id: int, deal_id: str, stage_code: str
    ) -> None:
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
