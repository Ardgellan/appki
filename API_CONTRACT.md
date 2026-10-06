# Appki MVP contract

This is the integration contract for the first implementation. All public-facing copy is Russian. The initial catalog contains only the real Happ IPA. Missing credentials must produce a truthful unconfigured state, never a fake payment or a fake entitlement.

## HTTP API (same-origin)

- `GET /api/config` -> `{brand, bot_username, support_url, payments_mode, payments_configured, telegram_configured}`. Do not expose secret values.
- `GET /api/apps` -> `{items: App[]}`.
- `GET /api/apps/{slug}` -> App.
- App: `{slug,title,subtitle,description,version,size_bytes,minimum_ios,price_rub,category,features:string[],available:boolean}`. Initial slug `happ`. `price_rub` is decimal string; prices are test-only until owner configures live mode.
- `POST /api/auth/telegram` JSON `{init_data:string}` -> `{access_token, user:{id,first_name,username?},expires_at}`. Validate Telegram HMAC + age, issue an opaque random token. All private APIs use `Authorization: Bearer ...`.
- `GET /api/me` -> `{user:{id,first_name,username?}}`.
- `POST /api/orders` JSON `{app_slug:string,request_id:string}` -> Order. Per-user request_id makes order creation idempotent; the client must reuse it when retrying the same purchase. Requires configured YooKassa. Returns authoritative provider confirmation URL; no simulated success.
- `POST /api/orders/{id}/retry-payment` -> Order. Owner can retry an uncertain creation with the same stored provider idempotence key; requests older than 23 hours are refused. Never creates a second order.
- `GET /api/orders` -> `{items:Order[]}` owned by current user.
- `GET /api/orders/{id}` -> Order owned by current user. It may reconcile nonfinal payment state against YooKassa with reasonable throttling.
- `POST /api/orders/{id}/install-link` -> `{url,expires_at}` for owned, paid, non-refunded order; creates a 1h bearer installation grant.
- `GET /api/orders/{id}/activation` -> `{mode:'manual'|'credentials',apple_id?:string,password?:string,instructions:string,support_url:string}`. Only owned, paid order; never log contents. In default manual mode show honest support instructions, no invented credentials or codes.
- `POST /api/payments/yookassa/webhook`: verify event by fetching payment from YooKassa, check payment ID, amount, RUB currency, merchant recipient if provided, order metadata, test flag, succeeded+paid status; perform idempotent state update. Respond 200 to safely ignored duplicates/unrelated events, 503 on provider outage for retry.
- `GET /healthz` -> minimal health; no keys, paths, or personal data.
- errors JSON `{detail:string}` (prefer user-understandable Russian).

Order: `{id,app_slug,app_title,amount_rub,currency:'RUB',status:'pending'|'succeeded'|'canceled'|'refunded',payment_url?:string,created_at,paid_at?:string,test:boolean,can_install:boolean}`.

## Installation URLs

- `GET /install/{token}` serves a standalone Safari install page, no Telegram auth needed because token is a scoped bearer capability.
- `GET|HEAD /install/{token}/manifest.plist` contains the exact IPA bundle ID/build and absolute HTTPS asset URL.
- `GET|HEAD /install/{token}/app.ipa` streams the unchanged local file, supports byte ranges, no directory access. Missing/expired/revoked grant fails. Tokens are hashed in DB. No request-path logs, analytics, external assets or referrers on these routes.
- Public frontend routes: `/`, `/apps/happ`, `/orders`, `/orders/{id}`, `/help`, `/payment/return?order_id=...` (the return page must never itself mark a payment paid; browser without auth guides user back to Telegram).
- Frontend should work in Telegram native WebView and as read-only public preview in a normal browser. Do not create a demo user session in production. User-visible disabled checkout if Telegram authentication or payment config is unavailable.

## Ownership of implementation

- Backend agent: `backend/appki/config.py`, `db.py`, `auth.py`, `main.py`, `ota.py`, backend API/security/integration tests.
- Payments agent: `backend/appki/payments.py`, its tests. Contract below.
- Frontend agent: all `web/` files, responsive design and build.
- Root: bot, deployment, env, docs, integration, final QA.

## Payment adapter

Provide `YooKassaClient(shop_id,secret_key,mode='test',api_base='https://api.yookassa.ru/v3', client=None)` using httpx AsyncClient (client injection for tests only). Runtime must use official HTTPS API, never accept user API hosts.

`async create_payment(*, order_id:str, amount_rub:str, description:str, return_url:str, idempotence_key:str) -> dict` sends capture=true + confirmation.redirect + metadata.order_id; Basic Auth; Idempotence-Key header. Exact amounts, no floats.
`async get_payment(payment_id:str) -> dict` returns authoritative provider JSON.
`async close()` releases owned client.
`PaymentConfigurationError`, `PaymentProviderError` (safe messages; no key/raw payload/URL leaks), `PaymentValidationError`.
`validate_payment(payment:dict, *, order_id:str, payment_id:str|None, amount_rub:str, shop_id:str, mode:str) -> str` returns pending/succeeded/canceled only after validating id, metadata, amount/currency, test flag and recipient account_id when supplied. succeeded requires paid=true and captured amount consistency. Throws on mismatch. Creation may have pending + paid=false.

Refunds in MVP: reconcile canceled checkout; mark/refuse access for verified full refunds if implementing refund notifications. No automatic money movement/refund initiation without owner operation.
