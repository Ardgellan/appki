# Appki MVP contract

This is the integration contract for the current implementation. All public-facing copy is Russian. The configured catalog contains only the real Happ IPA; additional products in test fixtures are not public listings. Missing credentials must produce a truthful unconfigured state, never a fake payment or a fake entitlement.

## HTTP API (same-origin)

- `GET /api/config` -> `{brand, bot_username, support_url, payments_mode, payments_configured, telegram_configured}`. Do not expose secret values.
- `GET /api/apps` -> `{items: App[]}`.
- `GET /api/apps/{slug}` -> App.
- App: `{slug,title,subtitle,description,version,size_bytes,minimum_ios,price_rub,category,features:string[],available:boolean}`. Initial slug `happ`. `price_rub` is decimal string; prices are test-only until owner configures live mode.
- `POST /api/auth/telegram` JSON `{init_data:string}` -> `{access_token, user:{id,first_name,username?},expires_at}`. Validate Telegram HMAC + age, issue an opaque random token. All private APIs use `Authorization: Bearer ...`.
- `GET /api/me` -> `{user:{id,first_name,username?}}`.
- `POST /api/orders` JSON `{app_slugs:string[],request_id:string,expected_amount_rub?:string}` -> Order. Accepts 1–20 distinct catalog slugs; no quantities. Legacy `{app_slug:string,...}` remains supported; supply exactly one of `app_slug` and `app_slugs`. Slugs use `[a-z0-9-]{1,64}`; `request_id` uses `[A-Za-z0-9_-]{8,64}`. The server sums catalog prices with Decimal and saves each item's price. `expected_amount_rub`, when supplied, must be a decimal string with two fractional digits; a mismatch returns 409 before creating a payment. The current frontend always sends the confirmed cart total.
- Order creation is idempotent per user and `request_id`; repeat the same key, item set and expected total when retrying. A new key reuses a single unresolved order with the same cart, total and shop mode; unresolved overlapping carts are blocked rather than creating a second payment. Known overlapping payments are checked first. A changed selection or saved-order total returns 409. Requires configured YooKassa and available IPA files. Returns the provider confirmation URL when available; an old unknown attempt returns its saved order with `requires_payment_review`, without another provider creation.
- `POST /api/orders/{id}/retry-payment` -> Order. Owner can retry an uncertain creation with the same stored provider idempotence key; creation attempts older than 23 hours are refused. For a known pending payment, fetch its current provider state before returning a confirmation URL. Never creates a second order.
- `GET /api/orders?status=succeeded|pending|canceled|refunded` -> `{items:Order[]}` owned by current user, newest first, up to 100 matching orders. Omitting `status` retains the unfiltered list. Purchases and pending attempts are fetched separately so unpaid drafts do not displace purchased apps.
- `GET /api/orders/{id}` -> Order owned by current user. Pending payments with a provider ID may be reconciled against YooKassa, at most once per 5 seconds for ordinary sequential reads.
- `POST /api/orders/{id}/items/{slug}/install-link` -> `{url,expires_at}` for an item in an owned, paid order in the current payment mode. Creates a bearer grant scoped to that item, valid for the configured TTL (at most 1h).
- `GET /api/orders/{id}/items/{slug}/activation` -> Activation for an item in an owned, paid order in the current payment mode. Activation: `{mode:'manual'|'credentials',apple_id?:string,password?:string,instructions:string,support_url:string}`. Never log contents. Default manual mode returns support instructions, no invented credentials or codes.
- Legacy `POST /api/orders/{id}/install-link` and `GET /api/orders/{id}/activation` remain available for single-item orders. Multi-item orders must specify an item; otherwise 409.
- `PATCH /api/orders/{id}/items/{slug}/state` JSON `{installed?:boolean,activated?:boolean}` -> OrderItem. Requires ownership and a paid order in the current mode. At least one non-null strict boolean is required. `activated:true` requires an existing installation mark or `installed:true` in the same request; otherwise 409. `installed:false` clears both marks; `activated:false` clears only activation. An explicitly contradictory `{installed:false,activated:true}` is invalid (422). Repeating a true mark preserves its original timestamp. These are user-reported steps, not Apple confirmation or automated 2FA delivery.
- `POST /api/payments/yookassa/webhook`: verify event by fetching payment from YooKassa, check payment ID, amount, RUB currency, merchant recipient if provided, order metadata, test flag, succeeded+paid status; perform idempotent state update. Respond 200 to safely ignored duplicates/unrelated events, 503 on provider outage for retry.
- `GET /healthz` -> minimal health; no keys, paths, or personal data.
- errors JSON `{detail:string}` (prefer user-understandable Russian).

Order: `{id,app_slug,app_title,amount_rub,total_rub,items:OrderItem[],currency:'RUB',status:'pending'|'succeeded'|'canceled'|'refunded',payment_url:string|null,has_payment:boolean,can_retry_payment:boolean,requires_payment_review:boolean,created_at,paid_at:string|null,test:boolean,can_install:boolean}`.

`payment_url` is exposed only for a pending payment. `has_payment` means a provider ID is known. `can_retry_payment` permits recovery of an unknown creation in the current shop mode within the 23-hour retry window. An older unknown attempt has `requires_payment_review`; retain it for provider/webhook reconciliation, do not cancel it or create a replacement based on elapsed time. New browser request keys must not bypass an unresolved overlapping purchase.

OrderItem: `{app_slug,app_title,amount_rub,installed:boolean,activated:boolean,installed_at:string|null,activated_at:string|null,can_install:boolean}`. Money values are decimal strings; timestamps are ISO strings. `amount_rub` and `total_rub` are the same saved order total. Top-level `app_slug` (first sorted item) and `app_title` (combined titles) remain for compatibility; use `items` for individual purchases. Order `can_install` is true when at least one item can be installed. Installation eligibility requires verified payment, the current test/live mode and an available product file.

## Installation URLs

- `GET /install/{token}` serves a standalone Safari install page, no Telegram auth needed because token is a scoped bearer capability.
- `GET|HEAD /install/{token}/manifest.plist` contains the exact IPA bundle ID/build and absolute HTTPS asset URL.
- `GET|HEAD /install/{token}/app.ipa` streams the unchanged local file, supports byte ranges, no directory access. Missing/expired/revoked grant fails. Tokens are hashed in DB. No request-path logs, analytics, external assets or referrers on these routes.
- Frontend routes: `/` (catalog/cart), `/apps/{slug}` (catalog with product details), `/orders` (purchased apps only, with installation/activation), `/payment/{id}` (payment status), `/orders/{id}` (order), `/orders/{id}/apps/{slug}` (item installation/activation), `/how-it-works` (public explanation of purchase, installation and activation), `/help`, `/payment/return?order_id=...`. Currently the only product route is `/apps/happ`. Private pages require a Telegram session; the return page never itself marks a payment paid. Known pending payments are reconciled quietly while the app is open; no unpaid list or checkout entry point is shown in My Apps.
- Frontend should work in Telegram native WebView and as read-only public preview in a normal browser. Do not create a demo user session in production. User-visible disabled checkout if Telegram authentication or payment config is unavailable.
- The cart is stored in browser sessionStorage per user. Checkout shows selected items and the total, requires explicit confirmation, and preserves the request key for an uncertain purchase. The server is authoritative for prices, entitlements and saved installation/activation marks.

## Ownership of implementation

- Backend agent: `backend/appki/config.py`, `db.py`, `auth.py`, `main.py`, `ota.py`, backend API/security/integration tests.
- Payments agent: `backend/appki/payments.py`, its tests. Contract below.
- Frontend agent: all `web/` files, responsive design and build.
- Root: bot, deployment, env, docs, integration, final QA.

## Payment adapter

Provide `YooKassaClient(shop_id,secret_key,mode='test',api_base='https://api.yookassa.ru/v3', client=None)` using httpx AsyncClient (client injection for tests only). Runtime must use official HTTPS API, never accept user API hosts.

`async create_payment(*, order_id:str, amount_rub:str, description:str, return_url:str, idempotence_key:str) -> dict` sends capture=true + confirmation.redirect + metadata.order_id; Basic Auth; Idempotence-Key header. Exact amounts, no floats. In live mode it additionally sends `payment_method_data: {type:'sbp'}` for [YooKassa's SBP redirect checkout](https://yookassa.ru/developers/payment-acceptance/integration-scenarios/manual-integration/other/sbp). Test mode keeps the test checkout without forcing SBP: YooKassa's [test shop supports cards and the YooMoney wallet](https://yookassa.ru/developers/payment-acceptance/testing-and-going-live/testing). The UI identifies test payments explicitly; changing the interface does not enable live mode.
`async get_payment(payment_id:str) -> dict` returns authoritative provider JSON.
`async close()` releases owned client.
`PaymentConfigurationError`, `PaymentProviderError` (safe messages; no key/raw payload/URL leaks), `PaymentValidationError`.
`validate_payment(payment:dict, *, order_id:str, payment_id:str|None, amount_rub:str, shop_id:str, mode:str) -> str` returns pending/succeeded/canceled only after validating id, metadata, amount/currency, test flag and recipient account_id when supplied. succeeded requires paid=true and captured amount consistency. Throws on mismatch. Creation may have pending + paid=false.

Refunds: verified full-refund notifications mark the order refunded and revoke all its installation grants. Partial refunds retain access. No automatic money movement/refund initiation without owner operation.
