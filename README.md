# pravburo-reff-crm

Единственная граница агентской платформы с Bitrix24.

Сервис создаёт лиды, повторно читает сделки по входящему webhook, проверяет
`CATEGORY_ID == 2`, извлекает атрибуцию и передаёт команду начисления в
`pravburo-reff-bounty`.

## Common submodule

```bash
git clone --recurse-submodules https://github.com/zadkie1ll/pravburo-reff-crm.git
git submodule update --init --recursive
```

Общие контракты берутся из `common`; собственных миграций у CRM нет.

## Маршруты

- `POST /internal/leads`;
- `GET /internal/deals/{deal_id}/contact-phone`;
- `POST /webhooks/bitrix/deal-category`;
- `GET /health/live`, `GET /health/ready`.

Все `/internal/*` требуют `X-Internal-Token`. Webhook Bitrix требует отдельный
`X-Webhook-Secret`.

```bash
cp .env.example .env
uv sync
uv run pytest
docker compose up --build -d
```
