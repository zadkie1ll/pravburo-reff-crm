from fastapi.testclient import TestClient

from src.bitrix import BitrixGateway, extract_attribution_marker
from src.config import get_settings
from src.main import app
from src.routes import extract_bitrix_deal_id


def test_marker_and_bitrix_payload_parsing() -> None:
    marker = "Агент: Иван\n[pravburo-agent:v1;agent_id=123;application_id=456]"
    assert extract_attribution_marker(marker) == (123, 456)
    assert extract_bitrix_deal_id({"document_id[2]": "DEAL_777"}) == "777"
    assert extract_bitrix_deal_id({"data": {"FIELDS": {"ID": 888}}}) == "888"


def test_internal_lead_endpoint_requires_token() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/internal/leads",
            json={
                "application_id": 1,
                "agent_id": 2,
                "full_name": "Тестовый Клиент",
                "phone_normalized": "+79990000000",
            },
        )
    assert response.status_code == 401


def test_internal_lead_endpoint_calls_bitrix(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_service_token", "test-token")

    async def fake_create_lead(self, lead):
        del self
        assert lead.application_id == 1
        return "9001"

    monkeypatch.setattr(BitrixGateway, "create_lead", fake_create_lead)
    with TestClient(app) as client:
        response = client.post(
            "/internal/leads",
            headers={"X-Internal-Token": "test-token"},
            json={
                "application_id": 1,
                "agent_id": 2,
                "full_name": "Тестовый Клиент",
                "phone_normalized": "+79990000000",
            },
        )
    assert response.status_code == 200
    assert response.json() == {"lead_id": "9001"}
