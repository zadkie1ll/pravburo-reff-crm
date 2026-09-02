import asyncio

from fastapi.testclient import TestClient

from src.bitrix import BitrixGateway, LeadData, extract_attribution_marker
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
        assert lead.full_name == "Тестовый Клиент"
        assert lead.phone_normalized == "+79990000000"
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
    assert response.json() == {"status": "created", "lead_id": "9001"}


def test_bitrix_lead_payload_contains_name_and_phone(monkeypatch) -> None:
    captured: dict = {}

    async def fake_call(self, method, payload):
        del self
        captured["method"] = method
        captured["payload"] = payload
        return 9002

    monkeypatch.setattr(BitrixGateway, "_call", fake_call)
    lead_id = asyncio.run(
        BitrixGateway().create_lead(
            LeadData(
                application_id=1,
                agent_id=2,
                agent_name="Тестовый агент",
                full_name="Иванов Иван Иванович",
                phone_normalized="+79990000000",
            )
        )
    )

    assert lead_id == "9002"
    assert captured["method"] == "crm.lead.add"
    fields = captured["payload"]["fields"]
    assert fields["NAME"] == "Иванов Иван Иванович"
    assert fields["PHONE"] == [{"VALUE": "+79990000000", "VALUE_TYPE": "WORK"}]
