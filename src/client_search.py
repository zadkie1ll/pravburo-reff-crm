"""Read-only candidate search; provider pages are not application result pages."""

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.bitrix import BitrixError, BitrixGateway
from src.stage_names import deal_stage_names

LOOKUP_ERRORS = (BitrixError, httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError)
NAME_FIELDS = ("LAST_NAME", "NAME", "SECOND_NAME")
PERSON_SELECT = ["ID", *NAME_FIELDS, "PHONE"]
DEAL_SELECT = ["ID", "TITLE", "CONTACT_ID", "CATEGORY_ID", "STAGE_ID"]
MAX_DEAL_PAGES = 10
CATEGORY_LABELS = {0: "Основная", 2: "Сопровождение"}


def category_label(category: int | None) -> str:
    if category is None:
        return "Лид"
    return CATEGORY_LABELS.get(category, f"Воронка #{category}")


def name_tokens(query: str) -> list[str]:
    query = " ".join(query.split())
    tokens = query.split()
    if not 3 <= len(query) <= 200 or not 1 <= len(tokens) <= 3:
        raise ValueError("Enter 1–3 name tokens, 3–200 characters")
    # Reject LIKE wildcards, controls, punctuation and non-name input.
    if any(not all(c.isalpha() or c in "-'’" for c in token) for token in tokens):
        raise ValueError("Enter a name using letters, spaces, apostrophes or hyphens")
    if any(not any(c.isalpha() for c in token) for token in tokens):
        raise ValueError("Each name token must contain letters")
    return tokens


def full_name(person: dict[str, Any]) -> str:
    return " ".join(
        value for field in NAME_FIELDS if (value := str(person.get(field) or "").strip())
    )


def phone(person: dict[str, Any]) -> str | None:
    return (
        next(
            (
                str(p["VALUE"]).strip()
                for p in person.get("PHONE") or []
                if str(p.get("VALUE") or "").strip()
            ),
            None,
        )
        or None
    )


def matches(value: str, tokens: list[str]) -> bool:
    normalized = value.casefold().replace("ё", "е")
    return all(token.casefold().replace("ё", "е") in normalized for token in tokens)


def entity_id(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,64}", text) or int(text) <= 0:
        raise BitrixError("Invalid entity ID")
    return text


class ClientSearch:
    def __init__(self, gateway: BitrixGateway):
        self.gateway = gateway

    async def page(self, method: str, payload: dict) -> tuple[list[dict], bool]:
        envelope = await self.gateway._call_envelope(method, payload)
        rows = envelope.get("result")
        if method == "crm.item.list" and isinstance(rows, dict):
            rows = rows.get("items")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BitrixError("Invalid list response")
        start = payload.get("start", 0)
        next_offset = envelope.get("next")
        if next_offset is not None and int(next_offset) <= start:
            raise BitrixError("Invalid pagination")
        # total also protects against an omitted next flag. A full page without
        # either metadata is treated conservatively as potentially incomplete.
        more = next_offset is not None or (
            int(envelope["total"]) > start + len(rows) if "total" in envelope else len(rows) >= 50
        )
        return rows, more

    async def people(self, method: str, tokens: list[str], start: int) -> tuple[dict, bool]:
        people = {}
        more = False
        # Legacy contact.list explicitly does NOT support LOGIC. Query an anchor
        # in each name field and apply remaining tokens locally, in any order.
        for field in NAME_FIELDS:
            rows, has_more = await self.page(
                method,
                {
                    "filter": {f"%{field}": tokens[0]},
                    "select": PERSON_SELECT
                    + (["TITLE", "STATUS_ID"] if method == "crm.lead.list" else []),
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            more |= has_more
            for row in rows:
                if matches(full_name(row), tokens):
                    people[entity_id(row["ID"])] = row
        return people, more

    async def labels(self, rows: list[dict]) -> None:
        async def names(entity: str) -> tuple[str, dict[str, str]]:
            if entity == "DEAL_STAGE" or entity.startswith("DEAL_STAGE_"):
                category_id = (
                    0 if entity == "DEAL_STAGE" else int(entity.removeprefix("DEAL_STAGE_"))
                )
                return entity, await deal_stage_names(self.gateway, category_id)
            try:
                result = await self.gateway._call(
                    "crm.status.list", {"filter": {"ENTITY_ID": entity}}
                )
                return entity, {str(stage["STATUS_ID"]): str(stage["NAME"]) for stage in result}
            except LOOKUP_ERRORS:
                return entity, {}

        entities = sorted({self.status_entity(row) for row in rows})
        stages = dict(await asyncio.gather(*(names(entity) for entity in entities)))
        # Apply stage names before fetching optional pipeline labels, so a slow
        # category dictionary cannot discard successfully resolved stage names.
        for row in rows:
            row["stage_label"] = stages.get(self.status_entity(row), {}).get(
                row["stage_code"], "Название этапа недоступно"
            )
        try:
            async with asyncio.timeout(2):
                result = await self.gateway._call("crm.category.list", {"entityTypeId": 2})
                categories = {
                    int(category["id"]): str(category["name"]) for category in result["categories"]
                }
                for row in rows:
                    row["category_label"] = categories.get(
                        row["category_id"], category_label(row["category_id"])
                    )
        except (*LOOKUP_ERRORS, TimeoutError):
            pass

    @staticmethod
    def status_entity(row: dict) -> str:
        category = row["category_id"]
        if category is None:
            return "STATUS"
        return "DEAL_STAGE" if category == 0 else f"DEAL_STAGE_{category}"

    async def search(self, tokens: list[str], page: int) -> dict:
        parsed = urlsplit(self.gateway.settings.bitrix_webhook_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BitrixError("Invalid portal URL")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        portal = f"{parsed.scheme}://{host}"
        allowed = self.gateway.settings.client_category_ids
        start = (page - 1) * 50
        contacts, more = await self.people("crm.contact.list", tokens, start)
        leads, lead_more = await self.people("crm.lead.list", tokens, start)
        more |= lead_more
        deals = {}
        if contacts and allowed:
            # Drain ALL deal pages for this contact batch across both pipelines.
            # A pathological batch fails explicitly rather than returning a
            # truncated success that the next contact page cannot recover.
            for method in ("crm.deal.list", "crm.item.list"):
                for index in range(MAX_DEAL_PAGES):
                    payload = {
                        "filter": {"@CONTACT_ID": list(contacts), "@CATEGORY_ID": sorted(allowed)},
                        "select": DEAL_SELECT,
                        "order": {"ID": "ASC"},
                        "start": index * 50,
                    }
                    if method == "crm.item.list":
                        # Legacy CONTACT_ID is the primary contact. The universal
                        # filter covers all linked contacts without scanning CRM.
                        payload = {
                            "entityTypeId": 2,
                            "filter": {
                                "@contactIds": list(contacts),
                                "@categoryId": sorted(allowed),
                            },
                            "select": [
                                "id",
                                "title",
                                "contactId",
                                "contactIds",
                                "categoryId",
                                "stageId",
                            ],
                            "order": {"id": "ASC"},
                            "start": index * 50,
                        }
                    found, deal_more = await self.page(method, payload)
                    for deal in found:
                        if method == "crm.item.list":
                            deal = {
                                "ID": deal["id"],
                                "TITLE": deal.get("title"),
                                "CONTACT_ID": deal.get("contactId"),
                                "CONTACT_IDS": deal.get("contactIds") or [],
                                "CATEGORY_ID": deal["categoryId"],
                                "STAGE_ID": deal.get("stageId"),
                            }
                        deals[entity_id(deal["ID"])] = deal
                    if not deal_more:
                        break
                else:
                    raise BitrixError("Too many associated deal pages; narrow the name")
        if allowed:
            # Name-only deals without contacts are useful candidates too.
            found, title_more = await self.page(
                "crm.deal.list",
                {
                    "filter": {"%TITLE": tokens[0], "@CATEGORY_ID": sorted(allowed)},
                    "select": DEAL_SELECT,
                    "order": {"ID": "ASC"},
                    "start": start,
                },
            )
            more |= title_more
            for deal in found:
                if matches(str(deal.get("TITLE") or ""), tokens):
                    deals.setdefault(entity_id(deal["ID"]), deal)
        rows = []
        for deal_id, deal in deals.items():
            category = int(deal["CATEGORY_ID"])
            if category not in allowed:
                continue
            contact = next(
                (
                    contacts[str(cid)]
                    for cid in [deal.get("CONTACT_ID"), *(deal.get("CONTACT_IDS") or [])]
                    if str(cid) in contacts
                ),
                {},
            )
            rows.append(
                {
                    "kind": "deal",
                    "id": deal_id,
                    "title": str(deal.get("TITLE") or ""),
                    "full_name": full_name(contact) or str(deal.get("TITLE") or ""),
                    "phone": phone(contact),
                    "category_id": category,
                    "category_label": category_label(category),
                    "stage_code": str(deal.get("STAGE_ID") or ""),
                    "stage_label": "Название этапа недоступно",
                    "url": f"{portal}/crm/deal/details/{deal_id}/",
                }
            )
        for lead_id, lead in leads.items():
            rows.append(
                {
                    "kind": "lead",
                    "id": lead_id,
                    "title": str(lead.get("TITLE") or ""),
                    "full_name": full_name(lead),
                    "phone": phone(lead),
                    "category_id": None,
                    "category_label": "Лид",
                    "stage_code": str(lead.get("STATUS_ID") or ""),
                    "stage_label": "Название этапа недоступно",
                    "url": f"{portal}/crm/lead/details/{lead_id}/",
                }
            )
        if rows:
            try:
                async with asyncio.timeout(15):
                    await self.labels(rows)
            except TimeoutError:
                pass
        rows.sort(key=lambda row: (row["kind"], int(row["id"])))
        return {"rows": rows, "has_more": bool(more), "page": page}
