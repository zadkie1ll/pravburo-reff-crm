import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from src.bitrix import BitrixGateway
from src.client_search import MAX_DEAL_PAGES
from src.config import get_settings
from src.main import app

HEADERS = {"X-Internal-Token": "search-token"}
PERSON = {
    "ID": "11",
    "LAST_NAME": "Иванов",
    "NAME": "Иван",
    "SECOND_NAME": "Иванович",
    "PHONE": [{"VALUE": " "}, {"VALUE": "+79990000000"}],
}


def deal(id="1", category="0", contact="11", title="Договор"):
    return {
        "ID": id,
        "TITLE": title,
        "CATEGORY_ID": category,
        "CONTACT_ID": contact,
        "STAGE_ID": "NEW" if category == "0" else "C2:NEW",
    }


@pytest.fixture
def provider(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "internal_service_token", "search-token")
    monkeypatch.setattr(settings, "bitrix_client_category_ids", "0,2")
    monkeypatch.setattr(
        settings,
        "bitrix_webhook_url",
        "https://user:password@portal.test:8443/rest/123/secret/?token=private",
    )
    original = httpx.AsyncClient
    calls = []

    def install(responder):
        def handler(request):
            method = str(request.url).rsplit("/", 1)[-1].removesuffix(".json")
            payload = json.loads(request.content)
            calls.append((method, payload))
            assert method in {
                "crm.contact.list",
                "crm.lead.list",
                "crm.deal.list",
                "crm.item.list",
                "crm.category.list",
                "crm.status.list",
            }
            reply = responder(method, payload)
            if isinstance(reply, httpx.Response):
                return reply
            return httpx.Response(200, json=reply)

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
        )
        return calls

    return install


def empty(method):
    if method == "crm.item.list":
        return {"result": {"items": []}, "total": 0}
    if method == "crm.category.list":
        return {
            "result": {
                "categories": [{"id": 0, "name": "Продажи"}, {"id": 2, "name": "Сопровождение"}]
            }
        }
    return {"result": [], "total": 0}


def search(q="Иван Иванов", page=1, headers=HEADERS):
    with TestClient(app) as client:
        return client.get(
            "/internal/clients/search", params={"q": q, "page": page}, headers=headers
        )


def test_candidates_two_pipelines_secondary_contacts_leads_and_title(provider):
    def responder(method, payload):
        if method == "crm.contact.list":
            assert payload["select"] == ["ID", "LAST_NAME", "NAME", "SECOND_NAME", "PHONE"]
            assert "LOGIC" not in payload["filter"]
            return {"result": [PERSON, PERSON, {**PERSON, "ID": "12"}], "total": 3}
        if method == "crm.lead.list":
            return {
                "result": [
                    {**PERSON, "ID": "1", "TITLE": "Лид", "STATUS_ID": "NEW"},
                    {**PERSON, "ID": "2", "TITLE": "Лид 2", "STATUS_ID": "NEW"},
                ],
                "total": 2,
            }
        if method == "crm.deal.list":
            assert payload["filter"]["@CATEGORY_ID"] == [0, 2]
            if "@CONTACT_ID" in payload["filter"]:
                assert payload["filter"]["@CONTACT_ID"] == ["11", "12"]
                return {
                    "result": [deal(), deal(), deal("2", "2", "12"), deal("9", "9")],
                    "total": 4,
                }
            return {
                "result": [
                    deal("1", title="Иванов Иван"),
                    deal("4", contact=None, title="Иванов Иван"),
                ],
                "total": 2,
            }
        if method == "crm.item.list":
            assert payload["entityTypeId"] == 2
            assert payload["filter"] == {"@contactIds": ["11", "12"], "@categoryId": [0, 2]}
            return {
                "result": {
                    "items": [
                        {
                            "id": 3,
                            "title": "Вторичный контакт",
                            "contactId": 999,
                            "contactIds": [999, 12],
                            "categoryId": 2,
                            "stageId": "C2:NEW",
                        }
                    ]
                },
                "total": 1,
            }
        if method == "crm.status.list":
            entity = payload["filter"]["ENTITY_ID"]
            return {
                "result": [
                    {"STATUS_ID": "C2:NEW" if entity == "DEAL_STAGE_2" else "NEW", "NAME": "Начало"}
                ]
            }
        return empty(method)

    calls = provider(responder)
    response = search()
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result) == {"rows", "has_more", "page"}
    assert result["has_more"] is False
    assert result["page"] == 1
    rows = result["rows"]
    assert [(r["kind"], r["id"]) for r in rows] == [
        ("deal", "1"),
        ("deal", "2"),
        ("deal", "3"),
        ("deal", "4"),
        ("lead", "1"),
        ("lead", "2"),
    ]
    for row in rows:
        assert set(row) == {
            "kind",
            "id",
            "title",
            "full_name",
            "phone",
            "category_id",
            "category_label",
            "stage_code",
            "stage_label",
            "url",
        }
        assert row["url"] == f"https://portal.test/crm/{row['kind']}/details/{row['id']}/"
        assert row["stage_label"] == "Начало"
    assert rows[0]["category_label"] == "Продажи"
    assert rows[2]["full_name"] == "Иванов Иван Иванович"
    assert rows[2]["phone"] == "+79990000000"
    assert rows[3]["full_name"] == "Иванов Иван"
    assert rows[3]["phone"] is None
    assert rows[4]["category_id"] is None
    assert "secret" not in response.text and "password" not in response.text
    assert len(calls) <= 31


@pytest.mark.parametrize("q", ["Иванов Иван Иванович", "Иванович Иван Иванов", "Иван Иванов"])
def test_token_order(provider, q):
    def responder(method, payload):
        if method in {"crm.contact.list", "crm.lead.list"}:
            field, anchor = next(iter(payload["filter"].items()))
            found = [PERSON] if anchor.casefold() in PERSON[field[1:]].casefold() else []
            return {"result": found, "total": len(found)}
        if method == "crm.deal.list" and "@CONTACT_ID" in payload["filter"]:
            return {"result": [deal()], "total": 1}
        return empty(method)

    provider(responder)
    result = search(q).json()
    assert [(r["kind"], r["id"]) for r in result["rows"]] == [("deal", "1"), ("lead", "11")]


@pytest.mark.parametrize("stream", ["crm.contact.list", "crm.lead.list", "title"])
def test_provider_streams_start_offset_and_has_more(provider, stream):
    def responder(method, payload):
        active = method == stream or (stream == "title" and method == "crm.deal.list")
        if active:
            return {"result": [], "next": payload["start"] + 50, "total": 200}
        return empty(method)

    calls = provider(responder)
    response = search(page=2)
    assert response.json() == {"rows": [], "has_more": True, "page": 2}
    assert all(p["start"] == 50 for _, p in calls)


@pytest.mark.parametrize("stream", ["crm.deal.list", "crm.item.list"])
def test_drains_all_associated_deal_pages(provider, stream):
    def responder(method, payload):
        if method == "crm.contact.list":
            return {"result": [PERSON], "total": 1}
        associated = method == "crm.item.list" or "@CONTACT_ID" in payload.get("filter", {})
        if method == stream and associated:
            offset = payload["start"]
            ids = range(1, 51) if offset == 0 else [51]
            rows = [deal(str(i)) for i in ids]
            if method == "crm.item.list":
                rows = [{"id": i, "categoryId": 0, "contactIds": [11]} for i in ids]
            return {
                "result": {"items": rows} if method == "crm.item.list" else rows,
                "total": 51,
                **({"next": 50} if offset == 0 else {}),
            }
        return empty(method)

    calls = provider(responder)
    response = search()
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 51
    assert response.json()["has_more"] is False
    assert [
        p["start"]
        for m, p in calls
        if m == stream and (m == "crm.item.list" or "@CONTACT_ID" in p.get("filter", {}))
    ] == [0, 50]


@pytest.mark.parametrize(
    "query,page",
    [
        ("ab", 1),
        ("   ", 1),
        ("a" * 201, 1),
        ("one two three four", 1),
        ("%%%", 1),
        ("123", 1),
        ("---", 1),
        ("Иван", 0),
        ("Иван", 1001),
        ("Иван", "bad"),
    ],
)
def test_invalid_input_never_calls_bitrix(provider, query, page):
    calls = provider(lambda *args: pytest.fail("Must not call Bitrix"))
    assert search(query, page).status_code == 422
    assert calls == []


def test_auth_and_page_1000(provider):
    calls = provider(lambda method, payload: empty(method))
    assert search(headers={}).status_code == 401
    assert search(headers={"X-Internal-Token": "wrong"}).status_code == 401
    assert calls == []
    assert search(page=1000).json() == {"rows": [], "has_more": False, "page": 1000}
    assert all(p["start"] == 49950 for _, p in calls)


@pytest.mark.parametrize("failure", ["nonjson", "http", "api", "malformed", "timeout", "bad_id"])
def test_lookup_errors_are_sanitized(provider, failure):
    def responder(method, payload):
        if failure == "nonjson":
            return httpx.Response(200, text="secret password")
        if failure == "http":
            return httpx.Response(403, json={"error": "secret"})
        if failure == "api":
            return {"error": "ACCESS_DENIED", "error_description": "secret"}
        if failure == "malformed":
            return {"result": {"secret": "password"}}
        if failure == "timeout":
            raise httpx.ReadTimeout("secret password")
        return {"result": [{**PERSON, "ID": "../secret"}], "total": 1}

    provider(responder)
    response = search()
    assert response.status_code == 502
    assert response.json() == {"detail": "Bitrix lookup unavailable"}


@pytest.mark.parametrize("stream", ["crm.deal.list", "crm.item.list"])
def test_associated_deal_bound_fails_instead_of_truncating(provider, stream):
    def responder(method, payload):
        if method == "crm.contact.list":
            return {"result": [PERSON], "total": 1}
        if method == stream:
            return {
                "result": {"items": []} if method == "crm.item.list" else [],
                "next": payload["start"] + 50,
            }
        return empty(method)

    calls = provider(responder)
    assert search().status_code == 502
    assert len([m for m, _ in calls if m == stream]) == MAX_DEAL_PAGES
    assert len(calls) <= 31


@pytest.mark.parametrize("malformed", [False, True])
def test_dictionary_failures_preserve_candidates(provider, malformed):
    def responder(method, payload):
        if method == "crm.contact.list":
            return {"result": [PERSON], "total": 1}
        if method == "crm.deal.list" and "@CONTACT_ID" in payload["filter"]:
            return {"result": [deal(), deal("2", "2")], "total": 2}
        if method in {"crm.category.list", "crm.status.list"}:
            return {"result": None} if malformed else httpx.Response(503, text="secret")
        return empty(method)

    provider(responder)
    response = search()
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [r["category_label"] for r in rows] == ["Основная", "Сопровождение"]
    assert [r["stage_label"] for r in rows] == ["NEW", "C2:NEW"]


def test_gateway_call_still_unwraps_result_and_envelope_keeps_next(provider):
    provider(lambda method, payload: {"result": [], "next": 50, "total": 51})

    async def run():
        gateway = BitrixGateway()
        assert await gateway._call("crm.contact.list", {}) == []
        assert await gateway._call_envelope("crm.contact.list", {}) == {
            "result": [],
            "next": 50,
            "total": 51,
        }

    asyncio.run(run())


@pytest.mark.parametrize(
    "metadata,expected",
    [({}, True), ({"total": 50}, False), ({"total": 51}, True), ({"next": 50}, True)],
)
def test_full_provider_page_metadata(provider, metadata, expected):
    def responder(method, payload):
        if method == "crm.lead.list":
            # Deliberately nonmatching names: local filtering must not erase
            # the upstream continuation flag even when rows becomes empty.
            return {
                "result": [
                    {**PERSON, "NAME": "Пётр", "LAST_NAME": "Петров", "SECOND_NAME": "Петрович"}
                ]
                * 50,
                **metadata,
            }
        return empty(method)

    provider(responder)
    assert search().json() == {"rows": [], "has_more": expected, "page": 1}


def test_unknown_pipeline_fallback(provider, monkeypatch):
    from src.client_search import category_label

    assert category_label(7) == "Воронка #7"
    assert category_label(None) == "Лид"


@pytest.mark.parametrize("url", ["", "/rest/1/secret", "ftp://portal.test/rest/secret"])
def test_invalid_portal_does_not_call_bitrix(provider, monkeypatch, url):
    calls = provider(lambda *args: pytest.fail("Invalid portal must not reach Bitrix"))
    monkeypatch.setattr(get_settings(), "bitrix_webhook_url", url)
    assert search().status_code == 502
    assert calls == []
