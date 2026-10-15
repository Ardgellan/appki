from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import secrets
import time
import uuid
from urllib.parse import quote, urlsplit
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, ValidationError, model_validator

from .auth import InvalidTelegramData, token_hash, validate_init_data
from .catalog import ArtifactRegistry, legacy_artifact, load_products, resolve_artifact
from .config import Settings
from .db import Database
from .ota import INSTALL_PAGE_HEADERS, IPAFile, PRIVATE_HEADERS
from .payments import (PaymentConfigurationError, PaymentProviderError, PaymentValidationError,
                       YooKassaClient, validate_payment)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z') if value is not None else None


INSTALL_WINDOW_SECONDS = 12 * 3600
ACTIVATION_WINDOW_SECONDS = 6 * 3600
CODE_REQUEST_COOLDOWN_SECONDS = 60


class AuthBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=16384)


AppSlug = Annotated[str, Field(pattern=r'^[a-z0-9-]{1,64}$')]


class OrderBody(BaseModel):
    app_slug: AppSlug | None = None
    app_slugs: list[AppSlug] | None = Field(default=None, min_length=1, max_length=20)
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')
    expected_amount_rub: str | None = Field(default=None, pattern=r'^[0-9]{1,8}\.[0-9]{2}$')

    @model_validator(mode='after')
    def valid_selection(self):
        if (self.app_slug is None) == (self.app_slugs is None):
            raise ValueError('Specify app_slug or app_slugs')
        if self.app_slugs is not None and len(set(self.app_slugs)) != len(self.app_slugs):
            raise ValueError('Duplicate applications')
        return self

    @property
    def slugs(self):
        return sorted(self.app_slugs if self.app_slugs is not None else [self.app_slug])


class ItemStateBody(BaseModel):
    installed: StrictBool | None = None
    activated: StrictBool | None = None

    @model_validator(mode='after')
    def valid_update(self):
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError('Specify installation or activation state')
        return self


def create_app(settings: Settings | None = None, *, payment_client=None, catalog_items=None):
    try:
        cfg = settings or Settings()
    except ValidationError:
        raise RuntimeError('Invalid Appki configuration; check environment settings.') from None
    db = Database(cfg.database_path)
    ipa = IPAFile(cfg.happ_ipa_path, cfg.happ_ipa_sha256)
    registry = ArtifactRegistry(cfg.artifact_registry_path, cfg.artifact_root)
    legacy_products = catalog_items if catalog_items is not None else {'happ': {'title': 'Happ', 'ipa': ipa}}
    products, product_signature = {}, None

    def current_products():
        nonlocal products, product_signature
        if catalog_items is not None:
            return catalog_items
        definition = cfg.catalog_definition_path or Path(__file__).with_name('catalog.json')
        try:
            signature = []
            for path in (cfg.artifact_registry_path, definition):
                try:
                    stat = Path(path).stat()
                    signature.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
                except FileNotFoundError:
                    signature.append(None)
            signature.append(cfg.happ_price_rub)
            signature = tuple(signature)
            if signature != product_signature:
                products = load_products(cfg, registry)
                product_signature = signature
            return products
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, 'Каталог временно недоступен.') from None

    def save_artifact(conn, record, *, legacy=False):
        conn.execute('INSERT OR IGNORE INTO artifact_snapshots (id,record_json,legacy) VALUES (?,?,?)',
                     (record['id'], json.dumps(record, ensure_ascii=False, sort_keys=True), int(legacy)))

    def artifact_snapshot(artifact_id, conn=None):
        if conn is None:
            with db.connection() as connection:
                row = connection.execute('SELECT record_json,legacy FROM artifact_snapshots WHERE id=?',
                                         (artifact_id,)).fetchone()
        else:
            row = conn.execute('SELECT record_json,legacy FROM artifact_snapshots WHERE id=?', (artifact_id,)).fetchone()
        try:
            if row is not None:
                return json.loads(row['record_json']), bool(row['legacy'])
            return registry.get(artifact_id), False
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, 'Данные установки временно недоступны.') from None

    def artifact_file(record):
        try:
            return resolve_artifact(record)
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, 'Файл приложения временно недоступен.') from None

    def legacy_record(slug):
        product = legacy_products.get(slug)
        if product is None or product.get('ipa') is None:
            return None
        return legacy_artifact(slug, product)

    current_products()
    payment = payment_client
    if payment is None and cfg.payments_configured:
        try:
            payment = YooKassaClient(cfg.yookassa_shop_id, cfg.yookassa_secret_key, mode=cfg.yookassa_mode)
        except PaymentConfigurationError:
            payment = None
    rate_buckets = defaultdict(deque)

    def rate_limit(key, count=30, period=60):
        now = time.monotonic()
        bucket = rate_buckets[key]
        while bucket and bucket[0] < now - period:
            bucket.popleft()
        if len(bucket) >= count:
            raise HTTPException(429, 'Слишком много запросов. Повторите через минуту.')
        bucket.append(now)
        if len(rate_buckets) > 10000:
            for stale_key in list(rate_buckets):
                if not rate_buckets[stale_key] or rate_buckets[stale_key][-1] < now - period:
                    rate_buckets.pop(stale_key, None)

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        with db.connection(write=True) as conn:
            for slug, product in current_products().items():
                app_file = product.get('ipa')
                conn.execute('INSERT OR REPLACE INTO apps VALUES (?,?,?,?,?)',
                             (slug, product['title'], app_file.version if app_file is not None else '',
                              app_file.size if app_file is not None else 0,
                              int(app_file is not None and not product.get('coming_soon', False) and app_file.available)))
            for slug in legacy_products:
                record = legacy_record(slug)
                if record is None:
                    continue
                save_artifact(conn, record, legacy=True)
                # Old links and reported installation steps refer to the original
                # file, never to a newly published release for the same slug.
                conn.execute('UPDATE install_grants SET artifact_id=? WHERE app_slug=? AND artifact_id IS NULL',
                             (record['id'], slug))
                conn.execute('''UPDATE order_items SET artifact_id=? WHERE app_slug=? AND artifact_id IS NULL
                    AND EXISTS(
                        SELECT 1 FROM install_grants g WHERE g.order_id=order_items.order_id
                        AND g.app_slug=order_items.app_slug AND g.artifact_id=?)''',
                    (record['id'], slug, record['id']))
                if db.legacy_item_migration:
                    conn.execute('''UPDATE order_items SET artifact_id=? WHERE app_slug=? AND artifact_id IS NULL
                        AND (installed_at IS NOT NULL OR activated_at IS NOT NULL)''', (record['id'], slug))
            conn.execute('DELETE FROM sessions WHERE expires_at <= ?', (int(time.time()),))
            conn.execute('DELETE FROM install_grants WHERE expires_at <= ?', (int(time.time()),))
        yield
        if payment is not None:
            await payment.close()

    app = FastAPI(title='Appki', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.ipa = ipa
    app.state.settings = cfg
    app.state.artifact_registry = registry

    @app.middleware('http')
    async def security_headers(request, call_next):
        if request.method in ('POST', 'PUT', 'PATCH'):
            try:
                if int(request.headers.get('content-length', '0')) > 32768:
                    return JSONResponse({'detail': 'Слишком большой запрос.'}, status_code=413)
            except ValueError:
                return JSONResponse({'detail': 'Некорректный запрос.'}, status_code=400)
        if request.method in ('POST', 'PUT', 'PATCH'):
            # Bound chunked requests too, before FastAPI reads authentication or payment JSON.
            chunks = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > 32768:
                    return JSONResponse({'detail': 'Слишком большой запрос.'}, status_code=413)
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith(('/api/', '/install/')):
            response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        if response.headers.get('content-type', '').startswith('text/html') and not request.url.path.startswith('/install/'):
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'self'; form-action 'none'"
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo initData, passwords or malformed request contents.
        return JSONResponse({'detail': 'Проверьте данные запроса.'}, status_code=422)

    def current_user(request: Request):
        authorization = request.headers.get('authorization', '')
        if not re.fullmatch(r'Bearer [0-9a-f]{64}', authorization):
            raise HTTPException(401, 'Откройте сервис через Telegram, чтобы войти.')
        with db.connection() as conn:
            row = conn.execute('''SELECT u.id,u.first_name,u.username FROM users u
                JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?''',
                (token_hash(authorization[7:]), int(time.time()))).fetchone()
        if row is None:
            raise HTTPException(401, 'Сессия истекла. Откройте сервис через Telegram снова.')
        return dict(row)

    def catalog(slug):
        product = current_products().get(slug)
        if product is None:
            raise HTTPException(404, 'Приложение не найдено.')
        app_file = product.get('ipa')
        if product.get('artifact') is not None:
            app_file = artifact_file(product['artifact'])
        coming_soon = bool(product.get('coming_soon', False))
        available = app_file is not None and not coming_soon and app_file.refresh()
        public = {key: product[key] for key in ('title', 'subtitle', 'description', 'price_rub', 'category', 'features')
                  if key in product}
        public['coming_soon'] = coming_soon
        if coming_soon:
            public['price_rub'] = None
        return {**public,
                'slug': slug, 'version': app_file.version if app_file is not None else '',
                'size_bytes': app_file.size if app_file is not None else 0,
                'minimum_ios': app_file.minimum_ios if app_file is not None else '', 'available': available}

    def order_items(order_id):
        with db.connection() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM order_items WHERE order_id=? ORDER BY app_slug', (order_id,)).fetchall()]

    def delivery_window(item, order, *, now=None):
        now = int(time.time()) if now is None else now
        paid = order['status'] == 'succeeded'
        install_expires = ((order['paid_at'] if order['paid_at'] is not None else order['created_at']) +
                           INSTALL_WINDOW_SECONDS) if paid else None
        activation_expires = (item['installed_at'] + ACTIVATION_WINDOW_SECONDS
                              if item['installed_at'] is not None else None)
        if not paid or bool(order['test']) != (cfg.yookassa_mode == 'test'):
            status = 'unavailable'
        elif item['activated_at'] is not None:
            status = 'completed'
        elif activation_expires is not None:
            status = 'activate' if now < activation_expires else 'activation_expired'
        else:
            status = 'install' if now < install_expires else 'install_expired'
        return {'server_now': iso(now), 'install_expires_at': iso(install_expires),
                'activation_expires_at': iso(activation_expires), 'access_status': status}

    def require_delivery(item, order, phase, *, now=None):
        window = delivery_window(item, order, now=now)
        status = window['access_status']
        if status == 'unavailable':
            raise HTTPException(403, 'Доступ к заказу закрыт.')
        if status == 'completed':
            raise HTTPException(409, 'Активация завершена. Для другого устройства оформите новую покупку.')
        if phase == 'install':
            if item['installed_at'] is not None:
                raise HTTPException(409, 'Установка уже отмечена. Перейдите к активации или оформите новую покупку.')
            if status == 'install_expired':
                raise HTTPException(410, '12 часов на установку истекли. Оформите новую покупку.')
        else:
            if status == 'install_expired':
                raise HTTPException(410, '12 часов на установку истекли. Оформите новую покупку.')
            if item['installed_at'] is None:
                raise HTTPException(409, 'Сначала отметьте установку приложения.')
            if status == 'activation_expired':
                raise HTTPException(410, '6 часов на активацию истекли. Обратитесь в поддержку.')
        return window

    def code_request_metadata(item, *, now=None, conn=None):
        now = int(time.time()) if now is None else now
        query = '''SELECT id,requested_at FROM activation_code_requests
                   WHERE order_id=? AND app_slug=? ORDER BY requested_at DESC,id DESC LIMIT 1'''
        if conn is None:
            with db.connection() as connection:
                row = connection.execute(query, (item['order_id'], item['app_slug'])).fetchone()
        else:
            row = conn.execute(query, (item['order_id'], item['app_slug'])).fetchone()
        retry_at = row['requested_at'] + CODE_REQUEST_COOLDOWN_SECONDS if row is not None else None
        return {'id': row['id'] if row is not None else None,
                'status': 'manual_required' if row is not None else 'not_requested',
                'requested_at': iso(row['requested_at']) if row is not None else None,
                'retry_at': iso(retry_at), 'retry_after': max(0, retry_at - now) if retry_at else 0,
                'expires_at': iso(item['installed_at'] + ACTIVATION_WINDOW_SECONDS)
                              if item['installed_at'] is not None else None}

    def item_view(item, order):
        if item.get('artifact_id'):
            record, _ = artifact_snapshot(item['artifact_id'])
            app_file = artifact_file(record) if record is not None and record['app_slug'] == item['app_slug'] else None
        else:
            product = current_products().get(item['app_slug'])
            app_file = (product.get('ipa') if product is not None and not product.get('coming_soon', False) else None)
        window = delivery_window(item, order)
        can_install = window['access_status'] == 'install' and app_file is not None and app_file.available
        if window['access_status'] == 'install' and not can_install:
            window['access_status'] = 'unavailable'
        can_activate = window['access_status'] == 'activate'
        return {'app_slug': item['app_slug'], 'app_title': item['app_title'],
                'amount_rub': item['amount_rub'], 'installed': item['installed_at'] is not None,
                'activated': item['activated_at'] is not None,
                'installed_at': iso(item['installed_at']), 'activated_at': iso(item['activated_at']),
                **window, 'can_install': can_install, 'can_activate': can_activate,
                'can_request_code': can_activate and code_request_metadata(item)['retry_after'] == 0}

    def owned_order(order_id, user_id):
        with db.connection() as conn:
            row = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Заказ не найден.')
        return dict(row)

    def order_view(row):
        items = [item_view(item, row) for item in order_items(row['id'])]
        unknown_pending = (row['status'] == 'pending' and not row['payment_id'] and
                           bool(row['test']) == (cfg.yookassa_mode == 'test'))
        within_retry_window = int(time.time()) - row['created_at'] < 23 * 3600
        return {'id': row['id'], 'app_slug': row['app_slug'], 'app_title': row['app_title'],
                'amount_rub': row['amount_rub'], 'total_rub': row['amount_rub'], 'items': items,
                'currency': row['currency'], 'status': row['status'],
                'payment_url': row['payment_url'] if row['status'] == 'pending' else None,
                'has_payment': bool(row['payment_id']),
                'can_retry_payment': unknown_pending and within_retry_window,
                'requires_payment_review': unknown_pending and not within_retry_window,
                'created_at': iso(row['created_at']),
                'paid_at': iso(row['paid_at']), 'test': bool(row['test']),
                'can_install': any(item['can_install'] for item in items)}

    def record_payment(order, value):
        status = validate_payment(value, order_id=order['id'], payment_id=order['payment_id'],
                                  amount_rub=order['amount_rub'], shop_id=cfg.yookassa_shop_id,
                                  mode='test' if order['test'] else 'live')
        refunded = value.get('refunded_amount')
        if refunded is not None:
            try:
                refund_amount = Decimal(refunded['value'])
                if refunded['currency'] != 'RUB' or not refund_amount.is_finite() or refund_amount < 0:
                    raise ValueError('Invalid refund')
                if status == 'succeeded' and refund_amount >= Decimal(order['amount_rub']):
                    status = 'refunded'
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise PaymentValidationError('Invalid refund state') from exc
        confirmation = value.get('confirmation') or {}
        payment_url = confirmation.get('confirmation_url')
        if payment_url:
            parsed = urlsplit(payment_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise PaymentValidationError('Invalid confirmation URL')
        now = int(time.time())
        with db.connection(write=True) as conn:
            current = conn.execute('SELECT * FROM orders WHERE id=?', (order['id'],)).fetchone()
            if current['payment_id'] and current['payment_id'] != value['id']:
                raise PaymentValidationError('Payment ID mismatch')
            # Final states are monotonic; an old pending event cannot revoke a completed payment.
            final = current['status']
            if final == 'pending' or (final == 'succeeded' and status == 'refunded'):
                final = status
            conn.execute('''UPDATE orders SET payment_id=?,payment_url=COALESCE(?,payment_url),status=?,
                paid_at=CASE WHEN ? IN ('succeeded','refunded') THEN COALESCE(paid_at,?) ELSE paid_at END,
                checked_at=? WHERE id=?''',
                (value['id'], payment_url, final, final, now, now, order['id']))
            if final == 'refunded':
                conn.execute('UPDATE install_grants SET revoked=1 WHERE order_id=?', (order['id'],))
        return owned_order(order['id'], order['user_id'])

    async def reconcile(order, *, force=False):
        if payment is None or not order['payment_id'] or bool(order['test']) != (cfg.yookassa_mode == 'test'):
            return order
        if not force and (order['status'] != 'pending' or time.time() - order['checked_at'] < 5):
            return order
        value = await payment.get_payment(order['payment_id'])
        return record_payment(order, value)

    def paid_order(order_id, user_id):
        row = owned_order(order_id, user_id)
        if row['status'] != 'succeeded' or bool(row['test']) != (cfg.yookassa_mode == 'test'):
            raise HTTPException(403, 'Доступ появится после подтверждённой оплаты в текущем режиме магазина.')
        return row

    def paid_item(order_id, user_id, app_slug=None):
        row = paid_order(order_id, user_id)
        items = order_items(order_id)
        if app_slug is None:
            if len(items) != 1:
                raise HTTPException(409, 'Выберите приложение в заказе.')
            return row, items[0]
        item = next((item for item in items if item['app_slug'] == app_slug), None)
        if item is None:
            raise HTTPException(404, 'Приложение в заказе не найдено.')
        return row, item

    def pin_item_artifact(order_id, user_id, app_slug=None, *, phase):
        order, item = paid_item(order_id, user_id, app_slug)
        require_delivery(item, order, phase)
        selected, selected_legacy = None, False
        if not item['artifact_id']:
            product = current_products().get(item['app_slug'])
            if product is None or product.get('coming_soon', False) or product.get('ipa') is None:
                raise HTTPException(503, 'Установка и активация этого приложения пока недоступны.')
            selected = product.get('artifact')
            if selected is None:
                selected = legacy_artifact(item['app_slug'], product)
                selected_legacy = True
            if selected['app_slug'] != item['app_slug']:
                raise HTTPException(503, 'Данные установки временно недоступны.')
        with db.connection(write=True) as conn:
            order = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
            if order is None:
                raise HTTPException(404, 'Заказ не найден.')
            if order['status'] != 'succeeded' or bool(order['test']) != (cfg.yookassa_mode == 'test'):
                raise HTTPException(403, 'Доступ к заказу закрыт.')
            item = dict(conn.execute('SELECT * FROM order_items WHERE order_id=? AND app_slug=?',
                                     (order_id, item['app_slug'])).fetchone())
            require_delivery(item, order, phase)
            if item['artifact_id']:
                record, is_legacy = artifact_snapshot(item['artifact_id'], conn)
            else:
                save_artifact(conn, selected, legacy=selected_legacy)
                record, is_legacy = artifact_snapshot(selected['id'], conn)
                conn.execute('UPDATE order_items SET artifact_id=? WHERE order_id=? AND app_slug=?',
                             (record['id'], order_id, item['app_slug']))
                item['artifact_id'] = record['id']
            if record is None or record['app_slug'] != item['app_slug']:
                raise HTTPException(503, 'Данные установки временно недоступны.')
        return item, record, is_legacy

    def activation_profile(record, is_legacy):
        if is_legacy:
            if (not record['id'].startswith('legacy-') or record['source_account_id'] is not None
                    or record['activation_profile_id'] != 'legacy'):
                raise HTTPException(503, 'Инструкция активации временно недоступна. Обратитесь в поддержку.')
            result = {'mode': cfg.activation_mode, 'instructions': cfg.activation_instructions,
                      'support_url': cfg.support_url}
            if cfg.activation_mode == 'credentials':
                result.update(apple_id=cfg.activation_apple_id, password=cfg.activation_password)
            return result
        fallback = {'mode': 'manual', 'instructions': cfg.activation_instructions, 'support_url': cfg.support_url}
        try:
            path = Path(cfg.activation_profiles_path)
            if path.is_symlink():
                raise ValueError('Invalid profile file')
            if not path.is_file():
                return fallback
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('Invalid profile file')
            data = json.loads(path.read_text())
            profiles = data['profiles']
            if not isinstance(profiles, dict):
                raise ValueError('Invalid profiles')
            profile = profiles.get(record['activation_profile_id'])
            if profile is None:
                return fallback
            if (not isinstance(profile, dict) or not record['source_account_id'] or
                    profile.get('account_id') != record['source_account_id']):
                raise ValueError('Activation account mismatch')
            mode = profile.get('mode', 'manual')
            instructions, support = profile.get('instructions', cfg.activation_instructions), profile.get('support_url', cfg.support_url)
            if mode not in ('manual', 'credentials') or not isinstance(instructions, str) or not isinstance(support, str):
                raise ValueError('Invalid profile')
            result = {'mode': mode, 'instructions': instructions, 'support_url': support}
            if mode == 'credentials':
                apple_id, password = profile.get('apple_id'), profile.get('password')
                if not isinstance(apple_id, str) or not apple_id or not isinstance(password, str) or not password:
                    raise ValueError('Incomplete credentials')
                result.update(apple_id=apple_id, password=password)
            return result
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(503, 'Инструкция активации временно недоступна. Обратитесь в поддержку.') from None

    @app.get('/healthz')
    def health():
        with db.connection() as conn:
            conn.execute('SELECT 1').fetchone()
        return {'status': 'ok'}

    @app.get('/api/config')
    def public_config():
        return {'brand': cfg.appki_brand, 'bot_username': cfg.bot_username.lstrip('@'),
                'support_url': cfg.support_url, 'payments_mode': cfg.yookassa_mode,
                'payments_configured': payment is not None, 'telegram_configured': bool(cfg.bot_token)}

    @app.get('/api/apps')
    def apps():
        return {'items': [catalog(slug) for slug in current_products()]}

    @app.get('/api/apps/{slug}')
    def one_app(slug: str):
        return catalog(slug)

    @app.post('/api/auth/telegram')
    def authenticate(body: AuthBody, request: Request):
        rate_limit(('auth', request.client.host if request.client else 'unknown'))
        if not cfg.bot_token:
            raise HTTPException(503, 'Вход через Telegram ещё не настроен.')
        try:
            user = validate_init_data(body.init_data, cfg.bot_token, max_age=cfg.auth_max_age_seconds)
        except InvalidTelegramData:
            raise HTTPException(401, 'Не удалось подтвердить вход. Откройте приложение через Telegram снова.')
        now = int(time.time())
        expires = now + cfg.session_ttl_seconds
        token = secrets.token_hex(32)
        with db.connection(write=True) as conn:
            conn.execute('''INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                first_name=excluded.first_name,username=excluded.username,updated_at=excluded.updated_at''',
                (user['id'], user['first_name'], user.get('username'), now))
            conn.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash(token), user['id'], expires))
            conn.execute('DELETE FROM sessions WHERE expires_at<=?', (now,))
        return {'access_token': token, 'user': user, 'expires_at': iso(expires)}

    @app.get('/api/me')
    def me(user=Depends(current_user)):
        return {'user': user}

    def overlapping_pending(conn, user_id, slugs):
        placeholders = ','.join('?' for _ in slugs)
        return [dict(row) for row in conn.execute(f'''SELECT DISTINCT o.* FROM orders o
            JOIN order_items i ON i.order_id=o.id
            WHERE o.user_id=? AND o.test=? AND o.status='pending' AND i.app_slug IN ({placeholders})
            ORDER BY o.created_at,o.id''', (user_id, int(cfg.yookassa_mode == 'test'), *slugs)).fetchall()]

    def same_cart(conn, order_id, slugs):
        saved_slugs = [item['app_slug'] for item in conn.execute(
            'SELECT app_slug FROM order_items WHERE order_id=? ORDER BY app_slug', (order_id,)).fetchall()]
        return saved_slugs == slugs

    async def refresh_payment(order):
        try:
            return await reconcile(order, force=True)
        except (PaymentProviderError, PaymentConfigurationError):
            raise HTTPException(503, 'Не удалось обновить статус оплаты. Повторите позже.')
        except PaymentValidationError:
            raise HTTPException(502, 'Не удалось проверить оплату. Обратитесь в поддержку.')

    @app.post('/api/orders')
    async def create_order(body: OrderBody, user=Depends(current_user)):
        if payment is None:
            raise HTTPException(503, 'Приём оплаты ещё не настроен.')
        selected = [catalog(slug) for slug in body.slugs]
        if any(item['coming_soon'] or item.get('price_rub') is None for item in selected):
            raise HTTPException(503, 'Это приложение готовится к запуску. Оплата пока недоступна.')
        if not all(item['available'] for item in selected):
            raise HTTPException(503, 'Файл приложения временно недоступен. Оплата не создана.')
        total = format(sum((Decimal(item['price_rub']) for item in selected), Decimal('0')), '.2f')
        rate_limit(('orders', user['id']), 12)
        now = int(time.time())
        with db.connection() as conn:
            existing = conn.execute('SELECT id FROM orders WHERE user_id=? AND request_id=?',
                                    (user['id'], body.request_id)).fetchone()
            candidates = [] if existing else overlapping_pending(conn, user['id'], body.slugs)
        # Provider I/O stays outside the write transaction. Do not start another purchase
        # while an overlapping payment might already have completed.
        for candidate in candidates:
            if candidate['payment_id']:
                await refresh_payment(candidate)
        with db.connection(write=True) as conn:
            row = conn.execute('SELECT * FROM orders WHERE user_id=? AND request_id=?',
                               (user['id'], body.request_id)).fetchone()
            if row is None:
                if body.expected_amount_rub is not None and Decimal(body.expected_amount_rub) != Decimal(total):
                    raise HTTPException(409, 'Цена изменилась. Обновите корзину и подтвердите новую сумму.')
                overlaps = overlapping_pending(conn, user['id'], body.slugs)
                # A webhook can finish a previously observed draft while we await its
                # provider lookup; inspect those same rows again under the write lock.
                observed = [conn.execute('SELECT * FROM orders WHERE id=?', (item['id'],)).fetchone()
                            for item in candidates]
                completed = next((item for item in observed if item is not None and item['status'] == 'succeeded'
                                  and same_cart(conn, item['id'], body.slugs)
                                  and Decimal(item['amount_rub']) == Decimal(total)), None)
                if completed is not None:
                    row = completed
                elif any(item is not None and item['status'] == 'succeeded' for item in observed):
                    raise HTTPException(409, 'Оплата части приложений уже подтверждена. Проверьте покупки и обновите корзину перед новой оплатой.')
                elif overlaps:
                    matching = next((item for item in overlaps if same_cart(conn, item['id'], body.slugs)
                                     and Decimal(item['amount_rub']) == Decimal(total)), None)
                    if matching is None or len(overlaps) != 1:
                        raise HTTPException(409, 'Есть незавершённая попытка оплаты этих приложений. Уточните её статус в поддержке перед новой оплатой.')
                    row = matching
            if row is None:
                order_id = str(uuid.uuid4())
                conn.execute('''INSERT INTO orders
                    (id,user_id,app_slug,app_title,amount_rub,request_id,created_at,test)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (order_id, user['id'], selected[0]['slug'], ', '.join(item['title'] for item in selected), total,
                     body.request_id, now, int(cfg.yookassa_mode == 'test')))
                conn.executemany('''INSERT INTO order_items (order_id,app_slug,app_title,amount_rub)
                    VALUES (?,?,?,?)''',
                    [(order_id, item['slug'], item['title'], item['price_rub']) for item in selected])
                row = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            else:
                if not same_cart(conn, row['id'], body.slugs):
                    raise HTTPException(409, 'Этот идентификатор уже используется для другого заказа.')
                if (body.expected_amount_rub is not None and
                        Decimal(body.expected_amount_rub) != Decimal(row['amount_rub'])):
                    raise HTTPException(409, 'Сумма этой попытки оплаты отличается. Откройте сохранённый заказ.')
            order = dict(row)
        if (order['status'] == 'pending' and not order['payment_id'] and
                bool(order['test']) == (cfg.yookassa_mode == 'test') and
                int(time.time()) - order['created_at'] >= 23 * 3600):
            return order_view(order)
        return await ensure_payment(order)

    async def ensure_payment(order):
        if payment is None:
            raise HTTPException(503, 'Приём оплаты ещё не настроен.')
        if bool(order['test']) != (cfg.yookassa_mode == 'test'):
            raise HTTPException(409, 'Режим оплаты изменился. Создайте новый заказ.')
        if order['status'] != 'pending':
            return order_view(order)
        if order['payment_id']:
            return order_view(await refresh_payment(order))
        if not all(catalog(item['app_slug'])['available'] for item in order_items(order['id'])):
            raise HTTPException(503, 'Файл приложения временно недоступен. Оплата не создана.')
        with db.connection(write=True) as conn:
            # Apply the overlap guard to every creation path, including an original
            # request key and explicit recovery of a legacy draft.
            latest = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?',
                                  (order['id'], order['user_id'])).fetchone()
            if latest is None:
                raise HTTPException(404, 'Заказ не найден.')
            order = dict(latest)
            if order['status'] == 'pending' and not order['payment_id']:
                slugs = [item['app_slug'] for item in conn.execute(
                    'SELECT app_slug FROM order_items WHERE order_id=?', (order['id'],)).fetchall()]
                if any(item['id'] != order['id'] for item in overlapping_pending(conn, order['user_id'], slugs)):
                    raise HTTPException(409, 'Есть другая незавершённая попытка оплаты этих приложений. Уточните её статус в поддержке перед новой оплатой.')
        if order['status'] != 'pending':
            return order_view(order)
        if order['payment_id']:
            return order_view(await refresh_payment(order))
        # YooKassa retains idempotence keys for 24h. Never risk a second charge after an uncertain old attempt.
        if int(time.time()) - order['created_at'] >= 23 * 3600:
            raise HTTPException(409, 'Статус старой попытки оплаты нужно уточнить в поддержке. Не оплачивайте повторно.')
        try:
            value = await payment.create_payment(order_id=order['id'], amount_rub=order['amount_rub'],
                        description=f"{cfg.appki_brand}: доступ к установке {order['app_title']}"[:128], idempotence_key=order['id'],
                        return_url=f"{cfg.public_base_url}/payment/return?order_id={quote(order['id'])}")
            return order_view(record_payment(order, value))
        except (PaymentProviderError, PaymentConfigurationError):
            raise HTTPException(503, 'Платёжный сервис временно недоступен. Повторите эту же попытку позже.')
        except PaymentValidationError:
            raise HTTPException(502, 'Не удалось проверить ответ платёжного сервиса. Обратитесь в поддержку.')


    @app.post('/api/orders/{order_id}/retry-payment')
    async def retry_payment(order_id: str, user=Depends(current_user)):
        row = owned_order(order_id, user['id'])
        rate_limit(('orders', user['id']), 12)
        return await ensure_payment(row)

    @app.get('/api/orders')
    def orders(status: Literal['pending', 'succeeded', 'canceled', 'refunded'] | None = None,
               user=Depends(current_user)):
        with db.connection() as conn:
            if status is None:
                rows = conn.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 100',
                                    (user['id'],)).fetchall()
            else:
                rows = conn.execute('SELECT * FROM orders WHERE user_id=? AND status=? ORDER BY created_at DESC LIMIT 100',
                                    (user['id'], status)).fetchall()
        return {'items': [order_view(row) for row in rows]}

    @app.get('/api/orders/{order_id}')
    async def one_order(order_id: str, user=Depends(current_user)):
        row = owned_order(order_id, user['id'])
        try:
            row = await reconcile(row)
        except (PaymentProviderError, PaymentConfigurationError):
            raise HTTPException(503, 'Не удалось обновить статус оплаты. Повторите позже.')
        except PaymentValidationError:
            raise HTTPException(502, 'Не удалось проверить оплату. Обратитесь в поддержку.')
        return order_view(row)

    def issue_install_link(order_id, user_id, app_slug=None):
        item, record, _ = pin_item_artifact(order_id, user_id, app_slug, phase='install')
        app_file = artifact_file(record)
        if not app_file.refresh():
            raise HTTPException(503, 'Файл приложения временно недоступен.')
        rate_limit(('grants', user_id), 20)
        token = secrets.token_hex(32)
        with db.connection(write=True) as conn:
            # Check payment again inside the same transaction as capability creation.
            row = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
            if row is None or row['status'] != 'succeeded' or bool(row['test']) != (cfg.yookassa_mode == 'test'):
                raise HTTPException(403, 'Доступ к заказу закрыт.')
            pinned = conn.execute('SELECT * FROM order_items WHERE order_id=? AND app_slug=?',
                                  (order_id, item['app_slug'])).fetchone()
            if pinned is None or pinned['artifact_id'] != record['id']:
                raise HTTPException(403, 'Доступ к установке закрыт.')
            now = int(time.time())
            require_delivery(pinned, row, 'install', now=now)
            expires = min(now + cfg.install_ttl_seconds,
                          (row['paid_at'] if row['paid_at'] is not None else row['created_at']) + INSTALL_WINDOW_SECONDS)
            conn.execute('''INSERT INTO install_grants (token_hash,order_id,expires_at,revoked,app_slug,artifact_id)
                VALUES (?,?,?,0,?,?)''', (token_hash(token), order_id, expires, item['app_slug'], record['id']))
            conn.execute('DELETE FROM install_grants WHERE expires_at<=?', (int(time.time()),))
        return {'url': f'{cfg.public_base_url}/install/{token}', 'expires_at': iso(expires)}

    @app.post('/api/orders/{order_id}/install-link')
    def install_link(order_id: str, user=Depends(current_user)):
        return issue_install_link(order_id, user['id'])

    @app.post('/api/orders/{order_id}/items/{app_slug}/install-link')
    def item_install_link(order_id: str, app_slug: str, user=Depends(current_user)):
        return issue_install_link(order_id, user['id'], app_slug)

    def activation_for_item(order_id, user_id, app_slug=None):
        item, record, is_legacy = pin_item_artifact(order_id, user_id, app_slug, phase='activate')
        # Validate cached identity/provenance too, even when returning only an
        # activation instruction rather than opening a download stream.
        artifact_file(record)
        profile = activation_profile(record, is_legacy)
        # Recheck after reading operator configuration: completion/expiry must
        # close credential delivery too, not merely hide a frontend button.
        order, item = paid_item(order_id, user_id, item['app_slug'])
        window = require_delivery(item, order, 'activate')
        request = code_request_metadata(item)
        return {**profile, 'server_now': window['server_now'],
                'activation_expires_at': window['activation_expires_at'],
                'can_request_code': request['retry_after'] == 0, 'code_request': request}

    @app.get('/api/orders/{order_id}/activation')
    def activation(order_id: str, user=Depends(current_user)):
        return activation_for_item(order_id, user['id'])

    @app.get('/api/orders/{order_id}/items/{app_slug}/activation')
    def item_activation(order_id: str, app_slug: str, user=Depends(current_user)):
        return activation_for_item(order_id, user['id'], app_slug)

    def request_activation_code(order_id, user_id, app_slug=None):
        item, record, _ = pin_item_artifact(order_id, user_id, app_slug, phase='activate')
        artifact_file(record)
        with db.connection(write=True) as conn:
            order = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
            if order is None:
                raise HTTPException(404, 'Заказ не найден.')
            item = dict(conn.execute('SELECT * FROM order_items WHERE order_id=? AND app_slug=?',
                                     (order_id, item['app_slug'])).fetchone())
            now = int(time.time())
            window = require_delivery(item, order, 'activate', now=now)
            request = code_request_metadata(item, now=now, conn=conn)
            if request['retry_after'] == 0:
                # Metadata records an explicit request for manual support. There
                # is no collector, code value, or claim that Apple sent a code.
                conn.execute('INSERT INTO activation_code_requests VALUES (?,?,?,?)',
                             (str(uuid.uuid4()), order_id, item['app_slug'], now))
                request = code_request_metadata(item, now=now, conn=conn)
        return {'server_now': window['server_now'], 'activation_expires_at': window['activation_expires_at'],
                'can_request_code': request['retry_after'] == 0, 'code_request': request}

    @app.post('/api/orders/{order_id}/activation/code-request')
    def activation_code_request(order_id: str, user=Depends(current_user)):
        return request_activation_code(order_id, user['id'])

    @app.post('/api/orders/{order_id}/items/{app_slug}/activation/code-request')
    def item_activation_code_request(order_id: str, app_slug: str, user=Depends(current_user)):
        return request_activation_code(order_id, user['id'], app_slug)

    @app.patch('/api/orders/{order_id}/items/{app_slug}/state')
    def item_state(order_id: str, app_slug: str, body: ItemStateBody, user=Depends(current_user)):
        _, before = paid_item(order_id, user['id'], app_slug)
        if body.installed is False or body.activated is False:
            raise HTTPException(409, 'Отметки установки и активации нельзя сбросить. Для другого устройства оформите новую покупку.')
        if body.installed is True and before['installed_at'] is None:
            try:
                pin_item_artifact(order_id, user['id'], app_slug, phase='install')
            except HTTPException as exc:
                # Another identical request may have completed the first mark
                # between our read and artifact pin. Preserve true idempotence.
                if exc.status_code != 409:
                    raise
                _, latest = paid_item(order_id, user['id'], app_slug)
                if latest['installed_at'] is None:
                    raise
        with db.connection(write=True) as conn:
            row = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user['id'])).fetchone()
            if row is None:
                raise HTTPException(404, 'Заказ не найден.')
            if row['status'] != 'succeeded' or bool(row['test']) != (cfg.yookassa_mode == 'test'):
                raise HTTPException(403, 'Доступ к заказу закрыт.')
            item = conn.execute('SELECT * FROM order_items WHERE order_id=? AND app_slug=?',
                                (order_id, app_slug)).fetchone()
            if item is None:
                raise HTTPException(404, 'Приложение в заказе не найдено.')
            now = int(time.time())
            installed_at, activated_at = item['installed_at'], item['activated_at']
            if body.installed is True and installed_at is None:
                require_delivery(item, row, 'install', now=now)
                installed_at = now
                conn.execute('UPDATE install_grants SET revoked=1 WHERE order_id=? AND app_slug=?',
                             (order_id, app_slug))
            if body.activated is True and activated_at is None:
                if installed_at is None:
                    raise HTTPException(409, 'Сначала отметьте установку приложения.')
                require_delivery({**dict(item), 'installed_at': installed_at}, row, 'activate', now=now)
                activated_at = now
            # These are the owner's reported UI steps, not confirmation from Apple.
            conn.execute('UPDATE order_items SET installed_at=?,activated_at=? WHERE order_id=? AND app_slug=?',
                         (installed_at, activated_at, order_id, app_slug))
            updated = dict(item)
            updated.update(installed_at=installed_at, activated_at=activated_at)
        return item_view(updated, row)

    @app.post('/api/payments/yookassa/webhook')
    async def webhook(request: Request):
        if payment is None:
            raise HTTPException(503, 'Платёжный сервис ещё не настроен.')
        body = await request.body()
        if len(body) > 32768:
            raise HTTPException(413, 'Слишком большой запрос.')
        try:
            event = json.loads(body)
            obj = event.get('object', {})
            event_type = event.get('event')
            if event_type not in ('payment.succeeded', 'payment.canceled', 'payment.waiting_for_capture', 'refund.succeeded'):
                return {'ok': True}
            payment_id = obj.get('payment_id') if event_type == 'refund.succeeded' else obj.get('id')
            if not isinstance(payment_id, str) or not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', payment_id):
                return {'ok': True}
            metadata = obj.get('metadata') or {}
            order_id = metadata.get('order_id') if isinstance(metadata, dict) else None
            with db.connection() as conn:
                row = conn.execute('SELECT * FROM orders WHERE payment_id=?', (payment_id,)).fetchone()
                if row is None and isinstance(order_id, str) and len(order_id) <= 64:
                    row = conn.execute('SELECT * FROM orders WHERE id=? AND payment_id IS NULL', (order_id,)).fetchone()
            if row is None or bool(row['test']) != (cfg.yookassa_mode == 'test'):
                return {'ok': True}
            # Webhook body is only a hint. Fetch current state using merchant credentials.
            authoritative = await payment.get_payment(payment_id)
            if authoritative.get('id') != payment_id:
                return {'ok': True}
            record_payment(dict(row), authoritative)
        except (json.JSONDecodeError, AttributeError, TypeError, PaymentValidationError):
            return {'ok': True}
        except (PaymentProviderError, PaymentConfigurationError):
            raise HTTPException(503, 'Проверка платежа временно недоступна.')
        return {'ok': True}

    def check_grant(token: str, request: Request):
        if request.url.query or not re.fullmatch('[0-9a-f]{64}', token):
            raise HTTPException(404, 'Ссылка не найдена.')
        with db.connection() as conn:
            row = conn.execute('''SELECT g.expires_at,g.revoked,g.app_slug,g.artifact_id,g.order_id,
                o.status,o.test,o.paid_at,o.created_at,i.installed_at,i.activated_at FROM install_grants g
                JOIN orders o ON o.id=g.order_id
                JOIN order_items i ON i.order_id=g.order_id AND i.app_slug=g.app_slug
                WHERE g.token_hash=?''', (token_hash(token),)).fetchone()
        if row is None:
            raise HTTPException(404, 'Ссылка не найдена.')
        if (row['expires_at'] <= int(time.time()) or row['revoked'] or row['status'] != 'succeeded'
                or bool(row['test']) != (cfg.yookassa_mode == 'test')
                or delivery_window(row, row)['access_status'] != 'install'):
            raise HTTPException(410, 'Доступ по ссылке установки закрыт. Проверьте срок и статус покупки.')
        if row['artifact_id']:
            record, _ = artifact_snapshot(row['artifact_id'])
        else:
            record = legacy_record(row['app_slug'])
            if record is not None:
                with db.connection(write=True) as conn:
                    save_artifact(conn, record, legacy=True)
                    conn.execute('UPDATE install_grants SET artifact_id=? WHERE token_hash=? AND artifact_id IS NULL',
                                 (record['id'], token_hash(token)))
                    conn.execute('UPDATE order_items SET artifact_id=? WHERE order_id=? AND app_slug=? AND artifact_id IS NULL',
                                 (record['id'], row['order_id'], row['app_slug']))
        if record is None or record['app_slug'] != row['app_slug']:
            raise HTTPException(503, 'Файл приложения временно недоступен.')
        app_file = artifact_file(record)
        if not app_file.refresh():
            raise HTTPException(503, 'Файл приложения временно недоступен.')
        return app_file

    @app.api_route('/install/{token}', methods=['GET', 'HEAD'])
    def install_page(token: str, request: Request):
        app_file = check_grant(token, request)
        content = app_file.page(cfg.public_base_url, token).encode()
        return Response(content if request.method != 'HEAD' else b'', media_type='text/html',
                        headers={**INSTALL_PAGE_HEADERS, 'Content-Length': str(len(content))})

    @app.api_route('/install/{token}/manifest.plist', methods=['GET', 'HEAD'])
    def manifest(token: str, request: Request):
        app_file = check_grant(token, request)
        content = app_file.manifest(cfg.public_base_url, token)
        return Response(content if request.method != 'HEAD' else b'', media_type='application/xml',
                        headers={**PRIVATE_HEADERS, 'Content-Length': str(len(content))})

    @app.api_route('/install/{token}/app.ipa', methods=['GET', 'HEAD'])
    def app_file(token: str, request: Request):
        file = check_grant(token, request)
        return file.response(request)

    if (cfg.static_dir / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=cfg.static_dir / 'assets'), name='assets')

    @app.get('/{path:path}')
    def frontend(path: str):
        if path.startswith(('api/', 'install/', 'assets/')) or path in ('api', 'install', 'assets'):
            raise HTTPException(404, 'Страница не найдена.')
        # Explicit SPA allowlist prevents accidentally exposing config/IPA files or future routes.
        order_uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
        catalog_page = path.startswith('apps/') and path[5:] in current_products()
        item_page = re.fullmatch(rf'orders/{order_uuid}/apps/[a-z0-9-]{{1,64}}', path)
        payment_page = re.fullmatch(rf'payment/{order_uuid}', path)
        if (path not in ('', 'orders', 'help', 'how-it-works', 'payment/return') and not catalog_page
                and not re.fullmatch(r'orders/[0-9a-f-]{36}', path) and not item_page and not payment_page):
            raise HTTPException(404, 'Страница не найдена.')
        index = cfg.static_dir / 'index.html'
        if not index.is_file():
            return HTMLResponse('<html lang="ru"><meta charset="utf-8"><p>Аппки: интерфейс готовится к запуску.</p></html>', status_code=503)
        return FileResponse(index, headers={'Cache-Control': 'no-cache'})

    return app
