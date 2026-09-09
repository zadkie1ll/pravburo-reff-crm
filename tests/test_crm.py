import asyncio

from fastapi.testclient import TestClient
from pravburo_ref_common.models import RewardType

from src.bitrix import BitrixGateway, LeadData, extract_attribution_marker
from src.bounty_client import BountyClient
from src.config import get_settings
from src.main import app
from src.routes import extract_bitrix_deal_id
from src.site_client import SiteClient


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


def test_deal_category_webhook_syncs_stage_and_creates_advance_reward(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bitrix_webhook_secret", "webhook-secret")
    monkeypatch.setattr(settings, "bitrix_client_category_id", 10)

    async def fake_get_deal(self, deal_id):
        del self
        assert deal_id == "555"
        return {
            "CATEGORY_ID": "10",
            "STAGE_ID": "C10:PREPARATION",
            "SOURCE_DESCRIPTION": "Агент: Иван\n[pravburo-agent:v1;agent_id=123;application_id=456]",
        }

    stage_calls: list[dict] = []

    async def fake_update_deal_stage(self, *, application_id, deal_id, stage_code):
        del self
        stage_calls.append(
            {"application_id": application_id, "deal_id": deal_id, "stage_code": stage_code}
        )

    reward_calls: list[dict] = []

    async def fake_create_reward(self, *, deal_id, application_id, agent_id, reward_type):
        del self
        reward_calls.append(
            {
                "deal_id": deal_id,
                "application_id": application_id,
                "agent_id": agent_id,
                "reward_type": reward_type,
            }
        )
        return {"status": "created", "reward_id": 1}

    monkeypatch.setattr(BitrixGateway, "get_deal", fake_get_deal)
    monkeypatch.setattr(SiteClient, "update_deal_stage", fake_update_deal_stage)
    monkeypatch.setattr(BountyClient, "create_reward", fake_create_reward)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bitrix/deal-category",
            headers={"X-Webhook-Secret": "webhook-secret"},
            data={"document_id[2]": "DEAL_555"},
        )

    assert response.status_code == 200
    assert stage_calls == [
        {"application_id": 456, "deal_id": "555", "stage_code": "C10:PREPARATION"}
    ]
    assert reward_calls == [
        {
            "deal_id": "555",
            "application_id": 456,
            "agent_id": 123,
            "reward_type": RewardType.ADVANCE,
        }
    ]


def test_deal_category_webhook_accepts_secret_as_query_param(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bitrix_webhook_secret", "webhook-secret")
    monkeypatch.setattr(settings, "bitrix_client_category_id", 10)

    async def fake_get_deal(self, deal_id):
        del self
        assert deal_id == "555"
        return {
            "CATEGORY_ID": "10",
            "STAGE_ID": "C10:PREPARATION",
            "SOURCE_DESCRIPTION": "Агент: Иван\n[pravburo-agent:v1;agent_id=123;application_id=456]",
        }

    async def fake_update_deal_stage(self, *, application_id, deal_id, stage_code):
        del self, application_id, deal_id, stage_code

    async def fake_create_reward(self, *, deal_id, application_id, agent_id, reward_type):
        del self, deal_id, application_id, agent_id, reward_type
        return {"status": "created", "reward_id": 1}

    monkeypatch.setattr(BitrixGateway, "get_deal", fake_get_deal)
    monkeypatch.setattr(SiteClient, "update_deal_stage", fake_update_deal_stage)
    monkeypatch.setattr(BountyClient, "create_reward", fake_create_reward)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bitrix/deal-category?secret=webhook-secret",
            data={"document_id[2]": "DEAL_555"},
        )

    assert response.status_code == 200


def test_deal_category_webhook_rejects_wrong_query_secret(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bitrix_webhook_secret", "webhook-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bitrix/deal-category?secret=wrong",
            data={"document_id[2]": "DEAL_555"},
        )

    assert response.status_code == 401


def test_deal_category_webhook_ignores_non_reward_stage(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "bitrix_webhook_secret", "webhook-secret")
    monkeypatch.setattr(settings, "bitrix_client_category_id", 10)

    async def fake_get_deal(self, deal_id):
        del self
        return {
            "CATEGORY_ID": "10",
            "STAGE_ID": "C10:NEW",
            "SOURCE_DESCRIPTION": "Агент: Иван\n[pravburo-agent:v1;agent_id=123;application_id=456]",
        }

    async def fake_update_deal_stage(self, *, application_id, deal_id, stage_code):
        del self

    reward_calls: list[dict] = []

    async def fake_create_reward(self, **kwargs):
        del self
        reward_calls.append(kwargs)
        return {"status": "created", "reward_id": 1}

    monkeypatch.setattr(BitrixGateway, "get_deal", fake_get_deal)
    monkeypatch.setattr(SiteClient, "update_deal_stage", fake_update_deal_stage)
    monkeypatch.setattr(BountyClient, "create_reward", fake_create_reward)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bitrix/deal-category",
            headers={"X-Webhook-Secret": "webhook-secret"},
            data={"document_id[2]": "DEAL_555"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "stage_not_reward_trigger"}
    assert reward_calls == []


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
