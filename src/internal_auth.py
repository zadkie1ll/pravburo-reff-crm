import hmac

from fastapi import Header, HTTPException

from src.config import get_settings


def require_internal_token(
    token: str = Header(default="", alias="X-Internal-Token"),
) -> None:
    expected = get_settings().internal_service_token
    if not expected or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid internal service token")
