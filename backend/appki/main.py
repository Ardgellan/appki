from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import secrets
import time
import uuid
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .auth import InvalidTelegramData, token_hash, validate_init_data
from .config import Settings
from .db import Database
from .ota import IPAFile, PRIVATE_HEADERS
from .payments import (PaymentConfigurationError, PaymentProviderError, PaymentValidationError,
                       YooKassaClient, validate_payment)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z') if value is not None else None


class AuthBody(BaseModel):
    init_data: str = Field(min_length=1, max_length=16384)


class OrderBody(BaseModel):
    app_slug: str = Field(pattern=r'^[a-z0-9-]{1,64}$')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,64}$')


def create_app(settings: Settings | None = None, *, payment_client=None):
    try:
        cfg = settings or Settings()
    except ValidationError:
        raise RuntimeError('Invalid Appki configuration; check environment settings.') from None
    db = Database(cfg.database_path)
    ipa = IPAFile(cfg.happ_ipa_path, cfg.happ_ipa_sha256)
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
            conn.execute('INSERT OR REPLACE INTO apps VALUES (?,?,?,?,?)',
                         ('happ', 'Happ', ipa.version, ipa.size, int(ipa.available)))
            conn.execute('DELETE FROM sessions WHERE expires_at <= ?', (int(time.time()),))
            conn.execute('DELETE FROM install_grants WHERE expires_at <= ?', (int(time.time()),))
        yield
        if payment is not None:
            await payment.close()

    app = FastAPI(title='Appki', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.ipa = ipa
    app.state.settings = cfg

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

    def catalog():
        available = ipa.refresh()
        return {'slug': 'happ', 'title': 'Happ', 'subtitle': 'Ваши VPN-подписки в одном приложении',
                'description': 'Клиент для подключения через Xray. Установка оригинального приложения на iPhone с пошаговой помощью в активации. VPN-подписка приобретается отдельно.',
                'version': ipa.version, 'size_bytes': ipa.size, 'minimum_ios': ipa.minimum_ios,
                'price_rub': cfg.happ_price_rub, 'category': 'VPN',
                'features': ['Оригинальная сборка', 'Установка через Safari', 'Инструкция по активации'],
                'available': available}

    def owned_order(order_id, user_id):
        with db.connection() as conn:
            row = conn.execute('SELECT * FROM orders WHERE id=? AND user_id=?', (order_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Заказ не найден.')
        return dict(row)

    def order_view(row):
        return {'id': row['id'], 'app_slug': row['app_slug'], 'app_title': row['app_title'],
                'amount_rub': row['amount_rub'], 'currency': row['currency'], 'status': row['status'],
                'payment_url': row['payment_url'], 'created_at': iso(row['created_at']),
                'paid_at': iso(row['paid_at']), 'test': bool(row['test']),
                'can_install': row['status'] == 'succeeded' and ipa.available and bool(row['test']) == (cfg.yookassa_mode == 'test')}

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
        return {'items': [catalog()]}

    @app.get('/api/apps/{slug}')
    def one_app(slug: str):
        if slug != 'happ':
            raise HTTPException(404, 'Приложение не найдено.')
        return catalog()

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

    @app.post('/api/orders')
    async def create_order(body: OrderBody, user=Depends(current_user)):
        if payment is None:
            raise HTTPException(503, 'Приём оплаты ещё не настроен.')
        if body.app_slug != 'happ':
            raise HTTPException(404, 'Приложение не найдено.')
        if not ipa.refresh():
            raise HTTPException(503, 'Файл приложения временно недоступен. Оплата не создана.')
        rate_limit(('orders', user['id']), 12)
        now = int(time.time())
        with db.connection(write=True) as conn:
            row = conn.execute('SELECT * FROM orders WHERE user_id=? AND request_id=?',
                               (user['id'], body.request_id)).fetchone()
            if row is None:
                order_id = str(uuid.uuid4())
                conn.execute('''INSERT INTO orders
                    (id,user_id,app_slug,app_title,amount_rub,request_id,created_at,test)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (order_id, user['id'], body.app_slug, 'Happ', cfg.happ_price_rub,
                     body.request_id, now, int(cfg.yookassa_mode == 'test')))
                row = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            order = dict(row)
        if order['app_slug'] != body.app_slug:
            raise HTTPException(409, 'Этот идентификатор уже используется для другого заказа.')
        return await ensure_payment(order)

    async def ensure_payment(order):
        if payment is None:
            raise HTTPException(503, 'Приём оплаты ещё не настроен.')
        if not ipa.refresh():
            raise HTTPException(503, 'Файл приложения временно недоступен. Оплата не создана.')
        if bool(order['test']) != (cfg.yookassa_mode == 'test'):
            raise HTTPException(409, 'Режим оплаты изменился. Создайте новый заказ.')
        if order['status'] != 'pending' or order['payment_id']:
            return order_view(order)
        # YooKassa retains idempotence keys for 24h. Never risk a second charge after an uncertain old attempt.
        if int(time.time()) - order['created_at'] >= 23 * 3600:
            raise HTTPException(409, 'Статус старой попытки оплаты нужно уточнить в поддержке. Не оплачивайте повторно.')
        try:
            value = await payment.create_payment(order_id=order['id'], amount_rub=order['amount_rub'],
                        description=f"{cfg.appki_brand}: доступ к установке Happ", idempotence_key=order['id'],
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
    def orders(user=Depends(current_user)):
        with db.connection() as conn:
            rows = conn.execute('SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 100',
                                (user['id'],)).fetchall()
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

    @app.post('/api/orders/{order_id}/install-link')
    def install_link(order_id: str, user=Depends(current_user)):
        paid_order(order_id, user['id'])
        if not ipa.refresh():
            raise HTTPException(503, 'Файл приложения временно недоступен.')
        rate_limit(('grants', user['id']), 20)
        token = secrets.token_hex(32)
        expires = int(time.time()) + cfg.install_ttl_seconds
        with db.connection(write=True) as conn:
            # Check payment again inside the same transaction as capability creation.
            row = conn.execute('SELECT status,test FROM orders WHERE id=? AND user_id=?', (order_id, user['id'])).fetchone()
            if row is None or row['status'] != 'succeeded' or bool(row['test']) != (cfg.yookassa_mode == 'test'):
                raise HTTPException(403, 'Доступ к заказу закрыт.')
            conn.execute('INSERT INTO install_grants VALUES (?,?,?,0)', (token_hash(token), order_id, expires))
            conn.execute('DELETE FROM install_grants WHERE expires_at<=?', (int(time.time()),))
        return {'url': f'{cfg.public_base_url}/install/{token}', 'expires_at': iso(expires)}

    @app.get('/api/orders/{order_id}/activation')
    def activation(order_id: str, user=Depends(current_user)):
        paid_order(order_id, user['id'])
        result = {'mode': cfg.activation_mode, 'instructions': cfg.activation_instructions, 'support_url': cfg.support_url}
        if cfg.activation_mode == 'credentials':
            result.update(apple_id=cfg.activation_apple_id, password=cfg.activation_password)
        return result

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
            row = conn.execute('''SELECT g.expires_at,g.revoked,o.status,o.test FROM install_grants g
                JOIN orders o ON o.id=g.order_id WHERE g.token_hash=?''', (token_hash(token),)).fetchone()
        if row is None:
            raise HTTPException(404, 'Ссылка не найдена.')
        if (row['expires_at'] <= int(time.time()) or row['revoked'] or row['status'] != 'succeeded'
                or bool(row['test']) != (cfg.yookassa_mode == 'test')):
            raise HTTPException(410, 'Ссылка истекла. Получите новую в своём заказе.')
        if not ipa.refresh():
            raise HTTPException(503, 'Файл приложения временно недоступен.')

    @app.api_route('/install/{token}', methods=['GET', 'HEAD'])
    def install_page(token: str, request: Request):
        check_grant(token, request)
        content = ipa.page(cfg.public_base_url, token).encode()
        return Response(content if request.method != 'HEAD' else b'', media_type='text/html',
                        headers={**PRIVATE_HEADERS, 'Content-Length': str(len(content))})

    @app.api_route('/install/{token}/manifest.plist', methods=['GET', 'HEAD'])
    def manifest(token: str, request: Request):
        check_grant(token, request)
        content = ipa.manifest(cfg.public_base_url, token)
        return Response(content if request.method != 'HEAD' else b'', media_type='application/xml',
                        headers={**PRIVATE_HEADERS, 'Content-Length': str(len(content))})

    @app.api_route('/install/{token}/app.ipa', methods=['GET', 'HEAD'])
    def app_file(token: str, request: Request):
        check_grant(token, request)
        return ipa.response(request)

    if (cfg.static_dir / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=cfg.static_dir / 'assets'), name='assets')

    @app.get('/{path:path}')
    def frontend(path: str):
        if path.startswith(('api/', 'install/', 'assets/')) or path in ('api', 'install', 'assets'):
            raise HTTPException(404, 'Страница не найдена.')
        # Explicit SPA allowlist prevents accidentally exposing config/IPA files or future routes.
        if path not in ('', 'apps/happ', 'orders', 'help', 'payment/return') and not re.fullmatch(r'orders/[0-9a-f-]{36}', path):
            raise HTTPException(404, 'Страница не найдена.')
        index = cfg.static_dir / 'index.html'
        if not index.is_file():
            return HTMLResponse('<html lang="ru"><meta charset="utf-8"><p>Аппки: интерфейс готовится к запуску.</p></html>', status_code=503)
        return FileResponse(index, headers={'Cache-Control': 'no-cache'})

    return app
