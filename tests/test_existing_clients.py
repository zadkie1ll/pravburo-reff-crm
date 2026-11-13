import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from pravburo_ref_common.models import RewardType

from src.bitrix import FIRST_PAYMENT_FIELD_CODE, BitrixError, BitrixGateway, BitrixNotFound
from src.bounty_client import BountyClient
from src.config import get_settings
from src.main import app
from src.routes import MAIN_REWARD_STAGE
from src.site_client import SiteClient, SiteLookupError


@pytest.fixture
def internal_headers(monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_service_token", "test-token")
    monkeypatch.setattr(get_settings(), "bitrix_client_category_ids", "0,2")
    return {"X-Internal-Token": "test-token"}


@pytest.fixture
def mock_http(monkeypatch):
    original_client = httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs)
        )

    return install


def test_preview_reads_only_deal_and_contact(monkeypatch, internal_headers):
    calls = []

    async def fake_call(self, method, payload):
        calls.append((method, payload))
        if method == "crm.deal.get":
            return {
                "CATEGORY_ID": "2",
                "CONTACT_ID": "99",
                "STAGE_ID": MAIN_REWARD_STAGE,
                FIRST_PAYMENT_FIELD_CODE: "189000.00|RUB",
                "OPPORTUNITY": "189000",
                "SOURCE_DESCRIPTION": "[pravburo-agent:v1;agent_id=12;application_id=34]",
            }
        assert method == "crm.contact.get"
        return {
            "LAST_NAME": " Иванов ",
            "NAME": "Иван",
            "SECOND_NAME": "Иванович",
            "PHONE": [{"VALUE": ""}, {"VALUE": "+79990000000"}],
        }

    monkeypatch.setattr(BitrixGateway, "_call", fake_call)
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client", headers=internal_headers)
    assert response.status_code == 200
    assert response.json() == {
        "deal_id": "555",
        "full_name": "Иванов Иван Иванович",
        "phone": "+79990000000",
        "stage_code": MAIN_REWARD_STAGE,
        "category_id": 2,
        "first_payment": "189000.00",
        "paid_in_full": True,
        "attributed_agent_id": 12,
        "attributed_application_id": 34,
    }
    assert calls == [("crm.deal.get", {"id": "555"}), ("crm.contact.get", {"id": "99"})]


@pytest.mark.parametrize("contact_mode", ["no_id", "empty", "not_found", "http404"])
def test_preview_allows_missing_contact(monkeypatch, internal_headers, contact_mode):
    async def fake_call(self, method, payload):
        if method == "crm.deal.get":
            return {
                "CATEGORY_ID": "0",
                "CONTACT_ID": None if contact_mode == "no_id" else "99",
                "PHONE": [{"VALUE": "+79990000000"}],
                "TITLE": "Not a contact name",
            }
        assert contact_mode != "no_id"
        if contact_mode == "not_found":
            raise BitrixNotFound("not found")
        if contact_mode == "http404":
            response = httpx.Response(404, request=httpx.Request("POST", "https://bitrix.test"))
            response.raise_for_status()
        return {}

    monkeypatch.setattr(BitrixGateway, "_call", fake_call)
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client", headers=internal_headers)
    assert response.status_code == 200
    result = response.json()
    assert result["phone"] is None
    assert result["full_name"] is None
    assert result["first_payment"] is None
    assert result["paid_in_full"] is False
    assert result["attributed_agent_id"] is None
    assert result["attributed_application_id"] is None


@pytest.mark.parametrize("deal_id", ["0", "000", "-1", "1.5", "abc", "１", "1" * 65])
def test_preview_validates_id_before_bitrix(monkeypatch, internal_headers, deal_id):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid ID must not reach Bitrix")

    monkeypatch.setattr(BitrixGateway, "_call", forbidden)
    with TestClient(app) as client:
        response = client.get(
            f"/internal/deals/{deal_id}/existing-client", headers=internal_headers
        )
    assert response.status_code == 422


def test_preview_requires_authentication(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated request must not reach Bitrix")

    monkeypatch.setattr(BitrixGateway, "_call", forbidden)
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client")
    assert response.status_code == 401


def test_preview_accepts_64_digit_positive_id(monkeypatch, internal_headers):
    deal_id = "9" * 64

    async def fake_call(self, method, payload):
        assert method == "crm.deal.get"
        assert payload == {"id": deal_id}
        return {"CATEGORY_ID": "2"}

    monkeypatch.setattr(BitrixGateway, "_call", fake_call)
    with TestClient(app) as client:
        response = client.get(
            f"/internal/deals/{deal_id}/existing-client", headers=internal_headers
        )
    assert response.status_code == 200
    assert response.json()["deal_id"] == deal_id


@pytest.mark.parametrize("status", [404, 502, 200])
def test_preview_handles_non_json_upstream(monkeypatch, internal_headers, mock_http, status):
    monkeypatch.setattr(get_settings(), "bitrix_webhook_url", "https://bitrix.test/secret")
    mock_http(lambda request: httpx.Response(status, text="secret non-json upstream page"))
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client", headers=internal_headers)
    assert response.status_code == (404 if status == 404 else 502)
    assert "secret" not in response.text


@pytest.mark.parametrize("deal", [{}, {"CATEGORY_ID": "10"}])
def test_preview_missing_or_disallowed_deal(monkeypatch, internal_headers, deal):
    async def fake_call(self, method, payload):
        assert method == "crm.deal.get"
        return deal

    monkeypatch.setattr(BitrixGateway, "_call", fake_call)
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client", headers=internal_headers)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "NOT_FOUND", "error_description": "secret"}, 404),
        (404, {"error": "secret"}, 404),
        (403, {"error": "ACCESS_DENIED", "error_description": "secret"}, 502),
        (500, {"error": "secret"}, 502),
        (200, {"result": {"CATEGORY_ID": "bad"}}, 502),
        (200, {"result": []}, 404),
    ],
)
def test_preview_http_errors_are_sanitized(
    monkeypatch, internal_headers, mock_http, status, body, expected
):
    monkeypatch.setattr(get_settings(), "bitrix_webhook_url", "https://bitrix.test/secret")
    mock_http(lambda request: httpx.Response(status, json=body))
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client", headers=internal_headers)
    assert response.status_code == expected
    assert "secret" not in response.text


@pytest.mark.parametrize("failure", ["timeout", "contact_error"])
def test_preview_dependency_failures(monkeypatch, internal_headers, failure):
    async def fake_call(self, method, payload):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret webhook URL")
        if method == "crm.deal.get":
            return {"CATEGORY_ID": "2", "CONTACT_ID": "99"}
        raise BitrixError("secret upstream error")

    monkeypatch.setattr(BitrixGateway, "_call", fake_call)
    with TestClient(app) as client:
        response = client.get("/internal/deals/555/existing-client", headers=internal_headers)
    assert response.status_code == 502
    assert "secret" not in response.text


@pytest.mark.parametrize("status", [200, 404])
def test_site_resolve_existing_deal_http(monkeypatch, internal_headers, mock_http, status):
    monkeypatch.setattr(get_settings(), "site_service_url", "https://site.test/")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://site.test/internal/existing-clients/deals/555"
        assert request.headers["X-Internal-Token"] == "test-token"
        return httpx.Response(status, json={"agent_id": 12, "application_id": 34})

    mock_http(handler)
    result = asyncio.run(SiteClient().resolve_existing_deal("555"))
    assert result == ({"agent_id": 12, "application_id": 34} if status == 200 else None)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {}),
        (401, {}),
        (200, {}),
        (200, {"agent_id": True, "application_id": 34}),
        (200, {"agent_id": 12, "application_id": 0}),
        (200, None),
    ],
)
def test_site_resolver_rejects_unavailable_or_invalid_binding(mock_http, status, body):
    mock_http(lambda request: httpx.Response(status, json=body))
    with pytest.raises(SiteLookupError, match="Existing deal lookup failed"):
        asyncio.run(SiteClient().resolve_existing_deal("555"))


def test_site_resolver_timeout(mock_http):
    def handler(request):
        raise httpx.ConnectTimeout("secret", request=request)

    mock_http(handler)
    with pytest.raises(SiteLookupError):
        asyncio.run(SiteClient().resolve_existing_deal("555"))


@pytest.mark.parametrize("lookup", ["bound", "missing", "unavailable", "marker"])
def test_webhook_import_attribution(monkeypatch, lookup):
    monkeypatch.setattr(get_settings(), "bitrix_webhook_secret", "webhook-secret")
    monkeypatch.setattr(get_settings(), "bitrix_client_category_ids", "0,2")
    calls = []

    async def get_deal(self, deal_id):
        return {
            "CATEGORY_ID": "2",
            "STAGE_ID": MAIN_REWARD_STAGE,
            "OPPORTUNITY": "10000",
            FIRST_PAYMENT_FIELD_CODE: "10000|RUB",
            "SOURCE_DESCRIPTION": (
                "[pravburo-agent:v1;agent_id=12;application_id=34]" if lookup == "marker" else ""
            ),
        }

    async def resolve(self, deal_id):
        assert lookup != "marker", "Marker flow must not depend on site lookup availability"
        calls.append(("resolve", deal_id))
        if lookup == "unavailable":
            raise SiteLookupError("secret")
        return {"agent_id": 12, "application_id": 34} if lookup == "bound" else None

    async def stage(self, **kwargs):
        calls.append(("stage", kwargs))

    async def reward(self, **kwargs):
        calls.append(("reward", kwargs))
        return {"status": "created"}

    monkeypatch.setattr(BitrixGateway, "get_deal", get_deal)
    monkeypatch.setattr(SiteClient, "resolve_existing_deal", resolve)
    monkeypatch.setattr(SiteClient, "update_deal_stage", stage)
    monkeypatch.setattr(BountyClient, "create_reward", reward)
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/bitrix/deal-category",
            headers={"X-Webhook-Secret": "webhook-secret"},
            json={"deal_id": "555"},
        )
    if lookup == "unavailable":
        assert response.status_code == 502
        assert "secret" not in response.text
        assert calls == [("resolve", "555")]
    elif lookup == "missing":
        assert response.status_code == 200
        assert response.json() == {"status": "ignored", "reason": "no_agent_attribution"}
        assert calls == [("resolve", "555")]
    else:
        assert response.status_code == 200
        assert response.json()["status"] == "processed"
        if lookup == "bound":
            assert calls.pop(0) == ("resolve", "555")
        assert calls[0] == (
            "stage",
            {"application_id": 34, "deal_id": "555", "stage_code": MAIN_REWARD_STAGE},
        )
        assert [payload["reward_type"] for kind, payload in calls[1:]] == [
            RewardType.ADVANCE,
            RewardType.MAIN,
            RewardType.BONUS_FULL_PAYMENT,
        ]
        for kind, payload in calls[1:]:
            assert kind == "reward"
            assert payload["agent_id"] == 12
            assert payload["application_id"] == 34
            assert payload["deal_id"] == "555"
