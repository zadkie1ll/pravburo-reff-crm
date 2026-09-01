from fastapi import FastAPI

from src.config import get_settings
from src.routes import router

settings = get_settings()


app = FastAPI(title="pravburo-reff-crm", debug=settings.app_debug)
app.include_router(router)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "service": "pravburo-reff-crm"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    status = "ok" if settings.bitrix_webhook_url else "not_configured"
    return {"status": status, "service": "pravburo-reff-crm"}
