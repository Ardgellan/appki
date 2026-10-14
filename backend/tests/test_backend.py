import base64
import copy
import hashlib
import html
import hmac
import json
import plistlib
import shutil
import sqlite3
import subprocess
import time
import uuid
from urllib.parse import urlencode, urlsplit
import zipfile

from fastapi.testclient import TestClient
import pytest

from appki.auth import InvalidTelegramData, token_hash, validate_init_data
from appki.config import Settings
from appki.db import Database
from appki.main import create_app
from appki.ota import INSTALL_SCRIPT, IPAFile
from appki.payments import PaymentProviderError

BOT_TOKEN = '123456:local-test-only-token'


def init_data(user_id=111, *, at=None, token=BOT_TOKEN, **extra):
    data = {'auth_date': str(int(time.time()) if at is None else at),
            'user': json.dumps({'id': user_id, 'first_name': 'Тест', 'username': 'tester'}, ensure_ascii=False),
            **extra}
    check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class FakePayments:
    def __init__(self):
        self.values = {}
        self.keys = []
        self.gets = []
        self.fail_create = False
        self.fail_after_create = False
        self.fail_get = False
        self.closed = False

    async def create_payment(self, **kwargs):
        self.keys.append(kwargs['idempotence_key'])
        if self.fail_create:
            raise PaymentProviderError('temporary')
        for value in self.values.values():
            if value['metadata']['order_id'] == kwargs['order_id']:
                return copy.deepcopy(value)
        payment_id = str(uuid.uuid4())
        value = {'id': payment_id, 'status': 'pending', 'paid': False, 'test': True,
                 'amount': {'value': kwargs['amount_rub'], 'currency': 'RUB'},
                 'metadata': {'order_id': kwargs['order_id']}, 'recipient': {'account_id': '12345'},
                 'confirmation': {'type': 'redirect', 'confirmation_url': 'https://yoomoney.ru/checkout/test'}}
        self.values[payment_id] = value
        if self.fail_after_create:
            raise PaymentProviderError('timeout after provider persisted payment')
        return copy.deepcopy(value)

    async def get_payment(self, payment_id):
        self.gets.append(payment_id)
        if self.fail_get:
            raise PaymentProviderError('temporary')
        return copy.deepcopy(self.values[payment_id])

    async def close(self):
        self.closed = True


@pytest.fixture
def configured(tmp_path, monkeypatch):
    # Tests never read an operator's .env or production credentials.
    for name in list(__import__('os').environ):
        if name.upper() in Settings.model_fields or name.lower() in Settings.model_fields:
            monkeypatch.delenv(name)
    ipa_path = tmp_path / 'Happ.ipa'
    info = {'CFBundleIdentifier': 'su.ffg.happ', 'CFBundleVersion': '2609171647',
            'CFBundleShortVersionString': '5.9.0', 'MinimumOSVersion': '15.0'}
    with zipfile.ZipFile(ipa_path, 'w') as archive:
        archive.writestr('Payload/Happ.app/Info.plist', plistlib.dumps(info))
        archive.writestr('Payload/Happ.app/Happ', b'synthetic-test-only-binary' * 100)
    cfg = Settings(_env_file=None, database_path=tmp_path / 'db.sqlite3', static_dir=tmp_path / 'dist',
                   happ_ipa_path=ipa_path, happ_ipa_sha256=hashlib.sha256(ipa_path.read_bytes()).hexdigest(),
                   happ_price_rub='1.00',
                   bot_token=BOT_TOKEN, yookassa_shop_id='12345', yookassa_secret_key='test_local_fake_only',
                   public_base_url='https://example.test', support_url='https://t.me/example_support')
    fake = FakePayments()
    app = create_app(cfg, payment_client=fake)
    with TestClient(app) as client:
        yield cfg, fake, app, client


def login(client, user_id=111):
    response = client.post('/api/auth/telegram', json={'init_data': init_data(user_id)})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['access_token']}


def order(client, headers, request_id='unique-request-1'):
    response = client.post('/api/orders', headers=headers, json={'app_slug': 'happ', 'request_id': request_id})
    assert response.status_code == 200, response.text
    return response.json()


def paid(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    result = order(client, headers)
    payment_id = next(iter(fake.values))
    fake.values[payment_id].update(status='succeeded', paid=True)
    response = client.post('/api/payments/yookassa/webhook', json={'event': 'payment.succeeded', 'object': {'id': payment_id}})
    assert response.status_code == 200
    return headers, result, payment_id


def grant(configured):
    headers, result, payment_id = paid(configured)
    client = configured[-1]
    response = client.post(f"/api/orders/{result['id']}/install-link", headers=headers)
    assert response.status_code == 200
    path = urlsplit(response.json()['url']).path
    return headers, result, payment_id, path


def test_hmac_tampering_and_age():
    now = 10000
    valid = init_data(at=now)
    assert validate_init_data(valid, BOT_TOKEN, now=now)['id'] == 111
    for bad in [valid.replace('auth_date=10000', 'auth_date=10001'), init_data(at=now-3601),
                init_data(at=now+31), valid+'&auth_date=10000', init_data(token='wrong'), 'hash=wrong']:
        with pytest.raises(InvalidTelegramData):
            validate_init_data(bad, BOT_TOKEN, now=now)


def test_auth_sessions_hash_and_expire(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    assert client.get('/api/me', headers=headers).json()['user']['id'] == 111
    token = headers['Authorization'][7:]
    with app.state.db.connection(write=True) as conn:
        row = conn.execute('SELECT token_hash FROM sessions').fetchone()
        assert row[0] == token_hash(token) and row[0] != token
        conn.execute('UPDATE sessions SET expires_at=0')
    assert client.get('/api/me', headers=headers).status_code == 401
    assert client.get('/api/me').status_code == 401


def test_public_config_no_secrets_and_real_availability(configured):
    cfg, fake, app, client = configured
    body = client.get('/api/config').json()
    assert body['payments_mode'] == 'test' and body['payments_configured']
    assert 'secret' not in json.dumps(body) and BOT_TOKEN not in json.dumps(body)
    assert client.get('/api/apps/happ').json()['available']
    cfg.happ_ipa_path.unlink()
    unavailable = client.get('/api/apps/happ').json()
    assert not unavailable['available'] and not unavailable['coming_soon']
    assert client.post('/api/orders', headers=login(client), json={'app_slug':'happ','request_id':'valid-key-1'}).status_code == 503
    assert not fake.keys


def test_disabled_configuration_has_no_fake_checkout(tmp_path):
    cfg = Settings(_env_file=None, bot_token='', yookassa_shop_id='', yookassa_secret_key='',
                   database_path=tmp_path/'db', happ_ipa_path=tmp_path/'absent', static_dir=tmp_path/'dist')
    with TestClient(create_app(cfg)) as client:
        assert not client.get('/api/config').json()['payments_configured']
        assert client.post('/api/auth/telegram', json={'init_data': init_data()}).status_code == 503
        assert client.get('/healthz').json() == {'status':'ok'}


def test_idempotent_orders_and_cross_user_ownership(configured):
    cfg, fake, app, client = configured
    owner = login(client)
    outsider = login(client, 222)
    first = order(client, owner)
    assert order(client, owner)['id'] == first['id']
    assert len(fake.keys) == 1
    assert client.get(f"/api/orders/{first['id']}", headers=outsider).status_code == 404
    assert client.get(f"/api/orders/{first['id']}/activation", headers=outsider).status_code == 404
    assert client.post(f"/api/orders/{first['id']}/install-link", headers=outsider).status_code == 404
    assert client.get('/api/orders', headers=outsider).json()['items'] == []
    assert client.post(f"/api/orders/{first['id']}/install-link", headers=owner).status_code == 403
    assert order(client, outsider)['id'] != first['id']


def test_timeout_retry_keeps_idempotence_key_and_stops_after_window(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    fake.fail_create = True
    payload = {'app_slug':'happ','request_id':'retry-request'}
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 503
    fake.fail_create = False
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 200
    assert fake.keys[0] == fake.keys[1]
    payment_id = next(iter(fake.values))
    fake.values[payment_id].update(status='canceled', paid=False)
    result = client.get('/api/orders', headers=headers).json()['items'][0]
    assert client.post(f"/api/orders/{result['id']}/retry-payment", headers=headers).json()['status'] == 'canceled'
    fake.fail_create = True
    payload['request_id'] = 'old-request'
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 503
    with app.state.db.connection(write=True) as conn:
        conn.execute('UPDATE orders SET created_at=0 WHERE request_id=?', ('old-request',))
    calls = len(fake.keys)
    review = client.post('/api/orders', headers=headers, json=payload)
    assert review.status_code == 200 and review.json()['requires_payment_review']
    assert client.post(f"/api/orders/{review.json()['id']}/retry-payment", headers=headers).status_code == 409
    assert len(fake.keys) == calls


def test_forged_webhook_and_return_do_not_grant_access(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    result = order(client, headers)
    payment_id = next(iter(fake.values))
    forged = {'id': payment_id, 'status':'succeeded', 'paid':True, 'amount':{'value':'1.00','currency':'RUB'}}
    assert client.post('/api/payments/yookassa/webhook', json={'event':'payment.succeeded','object':forged}).status_code == 200
    assert client.get(f"/api/orders/{result['id']}", headers=headers).json()['status'] == 'pending'
    client.get('/payment/return', params={'order_id':result['id'], 'status':'succeeded'})
    assert client.post(f"/api/orders/{result['id']}/install-link", headers=headers).status_code == 403


@pytest.mark.parametrize('field,value', [('amount',{'value':'0.01','currency':'RUB'}), ('test',False),
    ('recipient',{'account_id':'99999'}), ('metadata',{'order_id':'different-order'})])
def test_authoritative_mismatch_is_rejected(configured, field, value):
    cfg, fake, app, client = configured
    headers = login(client)
    result = order(client, headers)
    payment_id = next(iter(fake.values))
    fake.values[payment_id].update(status='succeeded', paid=True)
    fake.values[payment_id][field] = value
    assert client.post('/api/payments/yookassa/webhook', json={'event':'payment.succeeded','object':{'id':payment_id}}).status_code == 200
    assert client.post(f"/api/orders/{result['id']}/install-link", headers=headers).status_code == 403


def test_paid_duplicate_events_and_refund_revoke_existing_grant(configured):
    cfg, fake, app, client = configured
    headers, result, payment_id, path = grant(configured)
    event = {'event':'payment.succeeded','object':{'id':payment_id}}
    assert client.post('/api/payments/yookassa/webhook', json=event).status_code == 200
    assert client.get(path).status_code == 200
    assert client.get(f"/api/orders/{result['id']}/activation", headers=headers).json()['mode'] == 'manual'
    fake.values[payment_id]['refunded_amount'] = {'value':'1.00','currency':'RUB'}
    event = {'event':'refund.succeeded','object':{'id':str(uuid.uuid4()),'payment_id':payment_id}}
    assert client.post('/api/payments/yookassa/webhook', json=event).status_code == 200
    assert client.get(path).status_code == 410
    assert client.post(f"/api/orders/{result['id']}/install-link", headers=headers).status_code == 403
    assert client.get(f"/api/orders/{result['id']}", headers=headers).json()['status'] == 'refunded'


def test_provider_outage_requests_retry(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    order(client, headers)
    fake.fail_get = True
    payment_id = next(iter(fake.values))
    assert client.post('/api/payments/yookassa/webhook', json={'event':'payment.succeeded','object':{'id':payment_id}}).status_code == 503


def test_manifest_file_range_and_head_are_exact(configured):
    cfg, fake, app, client = configured
    headers, result, payment_id, path = grant(configured)
    data = cfg.happ_ipa_path.read_bytes()
    page = client.get(path)
    assert 'itms-services://' in page.text
    assert page.headers['referrer-policy'] == 'no-referrer'
    manifest = plistlib.loads(client.get(path+'/manifest.plist').content)['items'][0]
    assert manifest['metadata']['bundle-identifier'] == 'su.ffg.happ'
    assert manifest['metadata']['bundle-version'] == '2609171647'
    assert manifest['assets'][0]['url'] == 'https://example.test'+path+'/app.ipa'
    assert client.get(path+'/app.ipa').content == data
    head = client.head(path+'/app.ipa', headers={'Range':'bytes=0-4'})
    assert head.status_code == 200 and head.content == b'' and int(head.headers['content-length']) == len(data)
    for value, expected in [('bytes=0-3',data[:4]),('bytes=-10',data[-10:]),('bytes=10-',data[10:])]:
        res = client.get(path+'/app.ipa', headers={'Range':value})
        assert res.status_code == 206 and res.content == expected
    assert client.get(path+'/app.ipa', headers={'Range':'bytes=0-3','If-Range':'mismatch'}).content == data
    for value in ['bytes=-0', 'bytes=9-4','bytes=0-1,3-4','bytes=9999999999-', 'bad']:
        assert client.get(path+'/app.ipa', headers={'Range':value}).status_code == 416
    assert client.post(path+'/app.ipa').status_code == 405


def test_grant_is_hashed_expired_and_exact_paths(configured):
    cfg, fake, app, client = configured
    headers, result, payment_id, path = grant(configured)
    token = path.rsplit('/',1)[-1]
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT token_hash FROM install_grants').fetchone()[0] == token_hash(token)
    for suffix in ['/../../.env','/.env','/app.ipa?extra=1','?extra=1','/manifest.plist/extra']:
        assert client.get(path+suffix).status_code == 404
    with app.state.db.connection(write=True) as conn:
        conn.execute('UPDATE install_grants SET expires_at=0')
    assert client.get(path).status_code == 410
    assert client.get(path+'/manifest.plist').status_code == 410
    assert client.get(path+'/app.ipa').status_code == 410


def test_ipa_tampering_blocks_download(configured):
    cfg, fake, app, client = configured
    headers, result, payment_id, path = grant(configured)
    with cfg.happ_ipa_path.open('ab') as stream:
        stream.write(b'changed')
    assert client.get(path+'/app.ipa').status_code == 503
    assert not client.get('/api/apps/happ').json()['available']


def test_validation_errors_do_not_echo_signed_data(configured):
    client = configured[-1]
    secret = 'PRIVATE_DATA_' * 2000
    response = client.post('/api/auth/telegram', json={'init_data':secret})
    assert response.status_code in (413,422) and 'PRIVATE_DATA' not in response.text


def test_settings_errors_hide_activation_secrets():
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, activation_mode='credentials', activation_apple_id='',
                 activation_password='SENTINEL_PASSWORD', happ_price_rub='1.00')
    assert 'SENTINEL_PASSWORD' not in str(caught.value)


def test_oversized_chunked_request_is_bounded(configured):
    client = configured[-1]
    response = client.post('/api/auth/telegram', content=(b'x'*16000 for _ in range(4)))
    assert response.status_code == 413


def test_pending_orders_cannot_be_retried_in_different_payment_mode(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    fake.fail_create = True
    payload = {'app_slug':'happ','request_id':'changed-mode-key'}
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 503
    cfg.yookassa_mode = 'live'
    fake.fail_create = False
    calls = len(fake.keys)
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 409
    assert len(fake.keys) == calls


def test_concurrent_checkout_reuses_same_order(configured):
    from concurrent.futures import ThreadPoolExecutor
    cfg, fake, app, client = configured
    headers = login(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: order(client, headers, 'concurrent-key'), range(4)))
    assert len({result['id'] for result in results}) == 1
    assert len(set(fake.keys)) == 1
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1


def test_credentials_only_visible_to_paid_owner(configured):
    cfg, fake, app, client = configured
    cfg.activation_mode = 'credentials'
    cfg.activation_apple_id = 'test@example.test'
    cfg.activation_password = 'local-test-only-secret'
    owner, result, payment_id = paid(configured)
    outsider = login(client,222)
    path = f"/api/orders/{result['id']}/activation"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=outsider).status_code == 404
    response = client.get(path, headers=owner)
    assert response.json()['password'] == cfg.activation_password
    assert 'no-store' in response.headers['cache-control']
    assert cfg.activation_password not in client.get('/api/config').text


def test_test_purchase_cannot_grant_live_access_after_mode_switch(configured):
    cfg, fake, app, client = configured
    owner, result, payment_id, path = grant(configured)
    cfg.yookassa_mode = 'live'
    order_path = f"/api/orders/{result['id']}"
    assert not client.get(order_path, headers=owner).json()['can_install']
    assert not client.get('/api/orders', headers=owner).json()['items'][0]['can_install']
    assert client.get(order_path+'/activation', headers=owner).status_code == 403
    assert client.post(order_path+'/install-link', headers=owner).status_code == 403
    assert client.get(path).status_code == 410
    assert client.get(path+'/app.ipa').status_code == 410
    assert client.get(path+'/manifest.plist').status_code == 410
    calls = len(fake.gets)
    assert client.post('/api/payments/yookassa/webhook', json={'event':'payment.succeeded','object':{'id':payment_id}}).status_code == 200
    assert len(fake.gets) == calls
    assert client.post('/api/orders',headers=owner,json={'app_slug':'happ','request_id':'unique-request-1'}).status_code == 409


def test_order_retry_endpoint_recovers_uncertain_creation_without_second_order(configured):
    cfg, fake, app, client = configured
    owner = login(client)
    outsider = login(client, 222)
    fake.fail_create = True
    payload = {'app_slug':'happ','request_id':'recoverable-key'}
    assert client.post('/api/orders', headers=owner, json=payload).status_code == 503
    result = client.get('/api/orders', headers=owner).json()['items'][0]
    path = f"/api/orders/{result['id']}/retry-payment"
    assert client.post(path, headers=outsider).status_code == 404
    fake.fail_create = False
    recovered = client.post(path, headers=owner)
    assert recovered.status_code == 200
    assert recovered.json()['id'] == result['id'] and recovered.json()['payment_url']
    assert len(set(fake.keys)) == 1
    assert len(client.get('/api/orders', headers=owner).json()['items']) == 1


def test_cart_checkout_preserves_legacy_contract_and_rejects_invalid_items(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    payload = {'app_slugs': ['happ'], 'request_id': 'cart-checkout', 'expected_amount_rub': '1.00'}
    response = client.post('/api/orders', headers=headers, json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result['app_slug'] == 'happ' and result['app_title'] == 'Happ'
    assert result['amount_rub'] == result['total_rub'] == '1.00'
    assert result['items'] == [{'app_slug': 'happ', 'app_title': 'Happ', 'amount_rub': '1.00',
                                'installed': False, 'activated': False, 'installed_at': None,
                                'activated_at': None, 'can_install': False}]
    assert order(client, headers, 'cart-checkout')['id'] == result['id']
    assert len(fake.keys) == 1
    for selection in [{}, {'app_slugs': []}, {'app_slugs': ['happ', 'happ']},
                      {'app_slug': 'happ', 'app_slugs': ['happ']}]:
        assert client.post('/api/orders', headers=headers,
                           json={**selection, 'request_id': 'invalid-cart'}).status_code == 422
    assert client.post('/api/orders', headers=headers,
                       json={'app_slugs': ['unconfigured'], 'request_id': 'unknown-cart'}).status_code == 404
    assert len(fake.keys) == 1
    cfg.happ_ipa_path.unlink()
    assert client.post('/api/orders', headers=headers,
                       json={'app_slugs': ['happ'], 'request_id': 'unavailable-cart'}).status_code == 503
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1


def test_cart_price_confirmation_prevents_new_checkout_and_preserves_retry_snapshot(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    payload = {'app_slugs': ['happ'], 'request_id': 'priced-cart', 'expected_amount_rub': '2.00'}
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 409
    assert fake.keys == []
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
    fake.fail_create = True
    payload['expected_amount_rub'] = '1.00'
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 503
    cfg.happ_price_rub = '2.00'
    fake.fail_create = False
    recovered = client.post('/api/orders', headers=headers, json=payload)
    assert recovered.status_code == 200 and recovered.json()['amount_rub'] == '1.00'
    assert len(set(fake.keys)) == 1
    payload['request_id'] = 'new-priced-cart'
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 409
    payload['expected_amount_rub'] = '2.00'
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 409
    payment_id = next(iter(fake.values))
    fake.values[payment_id].update(status='canceled', paid=False)
    result = client.post('/api/orders', headers=headers, json=payload).json()
    assert result['amount_rub'] == result['items'][0]['amount_rub'] == '2.00'
    assert len(set(fake.keys)) == 2


def test_item_steps_are_owner_reported_paid_only_and_idempotent(configured):
    cfg, fake, app, client = configured
    owner = login(client)
    outsider = login(client, 222)
    result = order(client, owner)
    base = f"/api/orders/{result['id']}/items/happ"
    assert client.patch(base+'/state', headers=owner, json={'installed': True}).status_code == 403
    assert client.patch(base+'/state', headers=outsider, json={'installed': True}).status_code == 404
    payment_id = next(iter(fake.values))
    fake.values[payment_id].update(status='succeeded', paid=True)
    client.post('/api/payments/yookassa/webhook', json={'event': 'payment.succeeded', 'object': {'id': payment_id}})
    assert client.post(base+'/install-link', headers=owner).status_code == 200
    assert client.get(base+'/activation', headers=owner).json()['mode'] == 'manual'
    assert not client.get(f"/api/orders/{result['id']}", headers=owner).json()['items'][0]['installed']
    assert client.patch(base+'/state', headers=owner, json={'activated': True}).status_code == 409
    installed = client.patch(base+'/state', headers=owner, json={'installed': True}).json()
    assert installed['installed'] and not installed['activated']
    assert client.patch(base+'/state', headers=owner, json={'installed': True}).json()['installed_at'] == installed['installed_at']
    activated = client.patch(base+'/state', headers=owner, json={'activated': True}).json()
    assert activated['installed'] and activated['activated'] and activated['activated_at']
    assert client.patch(base+'/state', headers=owner, json={'activated': True}).json()['activated_at'] == activated['activated_at']
    assert client.get('/api/orders', headers=owner).json()['items'][0]['items'][0]['activated']
    reset = client.patch(base+'/state', headers=owner, json={'installed': False}).json()
    assert not reset['installed'] and not reset['activated']
    assert reset['installed_at'] is None and reset['activated_at'] is None
    assert client.patch(base+'/state', headers=owner, json={'installed': True, 'activated': True}).json()['activated']
    for body in [{}, {'installed': None}, {'installed': 1}, {'activated': 'true'},
                 {'installed': False, 'activated': True}]:
        assert client.patch(base+'/state', headers=owner, json=body).status_code == 422
    assert client.patch(base+'/state', json={'installed': True}).status_code == 401
    assert client.post(base+'/install-link', headers=outsider).status_code == 404
    assert client.get(base+'/activation', headers=outsider).status_code == 404
    assert client.patch(base.replace('happ', 'unknown')+'/state', headers=owner,
                        json={'installed': True}).status_code == 404
    cfg.yookassa_mode = 'live'
    assert client.patch(base+'/state', headers=owner, json={'installed': True}).status_code == 403
    cfg.yookassa_mode = 'test'
    fake.values[payment_id]['refunded_amount'] = {'value': '1.00', 'currency': 'RUB'}
    client.post('/api/payments/yookassa/webhook', json={'event': 'refund.succeeded', 'object': {'payment_id': payment_id}})
    assert client.patch(base+'/state', headers=owner, json={'installed': True}).status_code == 403


def test_multi_item_checkout_has_one_payment_and_independent_steps_and_files(configured, tmp_path):
    cfg, _, original_app, _ = configured
    second_path = tmp_path/'second.ipa'
    with zipfile.ZipFile(second_path, 'w') as archive:
        archive.writestr('Payload/Happ.app/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': 'test.fixture.second', 'CFBundleVersion': 'second-fixture',
            'CFBundleShortVersionString': '1.0', 'MinimumOSVersion': '15.0'}))
        archive.writestr('Payload/Happ.app/Happ', b'isolated-second-application-fixture')
    second = IPAFile(second_path, hashlib.sha256(second_path.read_bytes()).hexdigest(),
                     expected_bundle_id='test.fixture.second', title='Second test fixture')
    isolated_cfg = cfg.model_copy(update={'database_path': tmp_path/'multi.sqlite3'})
    fake = FakePayments()
    catalog = {'happ': {'title': 'Happ', 'price_rub': '1.00', 'ipa': original_app.state.ipa},
               'second': {'title': 'Second test fixture', 'price_rub': '2.50', 'ipa': second}}
    with TestClient(create_app(isolated_cfg, payment_client=fake, catalog_items=catalog)) as client:
        assert all(not item['coming_soon'] for item in client.get('/api/apps').json()['items'])
        headers = login(client)
        payload = {'app_slugs': ['second', 'happ'], 'request_id': 'multi-cart', 'expected_amount_rub': '3.50'}
        response = client.post('/api/orders', headers=headers, json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result['amount_rub'] == '3.50'
        assert [item['amount_rub'] for item in result['items']] == ['1.00', '2.50']
        assert len(fake.keys) == 1
        payload['app_slugs'].reverse()
        assert client.post('/api/orders', headers=headers, json=payload).json()['id'] == result['id']
        assert len(fake.keys) == 1
        payload['app_slugs'] = ['happ']
        assert client.post('/api/orders', headers=headers, json=payload).status_code == 409
        payment_id = next(iter(fake.values))
        fake.values[payment_id].update(status='succeeded', paid=True)
        client.post('/api/payments/yookassa/webhook', json={'event': 'payment.succeeded', 'object': {'id': payment_id}})
        base = f"/api/orders/{result['id']}"
        assert client.post(base+'/install-link', headers=headers).status_code == 409
        assert client.get(base+'/activation', headers=headers).status_code == 409
        paths = {}
        for slug, expected in [('happ', cfg.happ_ipa_path.read_bytes()), ('second', second_path.read_bytes())]:
            grant = client.post(base+f'/items/{slug}/install-link', headers=headers)
            assert grant.status_code == 200
            paths[slug] = urlsplit(grant.json()['url']).path
            assert client.get(paths[slug]+'/app.ipa').content == expected
            metadata = plistlib.loads(client.get(paths[slug]+'/manifest.plist').content)['items'][0]['metadata']
            assert metadata['bundle-identifier'] == ('su.ffg.happ' if slug == 'happ' else 'test.fixture.second')
            assert metadata['title'] == catalog[slug]['title']
        assert client.get(base+'/items/second/activation', headers=headers).status_code == 200
        client.patch(base+'/items/second/state', headers=headers, json={'installed': True, 'activated': True})
        items = client.get(base, headers=headers).json()['items']
        assert not items[0]['installed'] and not items[0]['activated']
        assert items[1]['installed'] and items[1]['activated']
        fake.values[payment_id]['refunded_amount'] = {'value': '3.50', 'currency': 'RUB'}
        client.post('/api/payments/yookassa/webhook', json={'event': 'refund.succeeded', 'object': {'payment_id': payment_id}})
        assert all(client.get(path).status_code == 410 for path in paths.values())
        assert client.patch(base+'/items/second/state', headers=headers, json={'installed': True}).status_code == 403


def test_legacy_database_migration_preserves_orders_sessions_and_install_links(configured, tmp_path):
    cfg, _, _, _ = configured
    legacy_path = tmp_path/'legacy.sqlite3'
    token, grant_token, order_id = 'a'*64, 'b'*64, str(uuid.uuid4())
    now = int(time.time())
    with sqlite3.connect(legacy_path) as conn:
        conn.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, username TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL);
            CREATE TABLE orders (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), app_slug TEXT NOT NULL,
                app_title TEXT NOT NULL, amount_rub TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'RUB',
                status TEXT NOT NULL DEFAULT 'pending', request_id TEXT NOT NULL, payment_id TEXT UNIQUE, payment_url TEXT,
                created_at INTEGER NOT NULL, paid_at INTEGER, checked_at INTEGER NOT NULL DEFAULT 0,
                test INTEGER NOT NULL, UNIQUE(user_id,request_id));
            CREATE TABLE install_grants (token_hash TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
                expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        ''')
        conn.execute('INSERT INTO users VALUES (111,?,?,?)', ('Legacy owner', 'legacy', now))
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash(token), 111, now+3600))
        conn.execute('''INSERT INTO orders (id,user_id,app_slug,app_title,amount_rub,status,request_id,created_at,paid_at,test)
            VALUES (?,111,'happ','Happ','9.00','succeeded','legacy-request',?,?,1)''', (order_id, now-30, now-20))
        conn.execute('INSERT INTO install_grants VALUES (?,?,?,0)', (token_hash(grant_token), order_id, now+1800))
        before = conn.execute('SELECT * FROM orders').fetchone()
    isolated_cfg = cfg.model_copy(update={'database_path': legacy_path})
    headers = {'Authorization': 'Bearer '+token}
    for _ in range(2):
        with TestClient(create_app(isolated_cfg, payment_client=FakePayments())) as client:
            result = client.get(f'/api/orders/{order_id}', headers=headers).json()
            assert result['amount_rub'] == result['items'][0]['amount_rub'] == '9.00'
            assert not result['items'][0]['installed'] and not result['items'][0]['activated']
            assert client.get(f'/install/{grant_token}/app.ipa').content == cfg.happ_ipa_path.read_bytes()
        with Database(legacy_path).connection() as conn:
            assert tuple(conn.execute('SELECT * FROM orders').fetchone()) == before
            assert conn.execute('SELECT COUNT(*) FROM order_items').fetchone()[0] == 1
            assert conn.execute('SELECT app_slug FROM install_grants').fetchone()[0] == 'happ'


def test_spa_payment_and_item_routes_serve_only_public_shell(configured):
    cfg, fake, app, client = configured
    cfg.static_dir.mkdir()
    shell = '<!doctype html><html><body>public application shell</body></html>'
    (cfg.static_dir/'index.html').write_text(shell)
    order_id = str(uuid.uuid4())
    for path in ['/', '/orders', '/help', '/how-it-works', '/apps/happ', '/payment/return', f'/orders/{order_id}',
                 f'/payment/{order_id}', f'/orders/{order_id}/apps/happ',
                 f'/orders/{order_id}/apps/historical-app']:
        response = client.get(path)
        assert response.status_code == 200 and response.text == shell
    for path in ['/apps/unconfigured', '/payment/not-a-uuid', f'/payment/{order_id}/extra',
                 '/orders/not-a-uuid/apps/happ', f'/orders/{order_id}/apps/happ/extra',
                 f'/orders/{order_id}/apps/invalid_slug', '/.env', '/api/unmatched', '/install/unmatched']:
        assert client.get(path).status_code == 404
    assert fake.keys == [] and fake.gets == []
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0


def test_ipa_metadata_uses_pinned_bundle_and_safely_escaped_title(tmp_path):
    path = tmp_path/'other.ipa'
    bundle_id = 'test.fixture.other'
    title = '<script>alert("title")</script> & Another app'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Payload/Other.app/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': bundle_id, 'CFBundleVersion': '42', 'CFBundleShortVersionString': '2.0'}))
        archive.writestr('Payload/Other.app/Other', b'isolated-ota-fixture')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert not IPAFile(path, digest).available
    app_file = IPAFile(path, digest, expected_bundle_id=bundle_id, title=title)
    assert app_file.available
    manifest = app_file.manifest('https://example.test', 'a'*64)
    metadata = plistlib.loads(manifest)['items'][0]['metadata']
    assert metadata['bundle-identifier'] == bundle_id and metadata['title'] == title
    assert b'<script>' not in manifest
    page = app_file.page('https://example.test', 'a'*64)
    assert title not in page and page.count('<script>') == page.count('</script>') == 1
    assert page.split('<script>')[1].split('</script>')[0] == INSTALL_SCRIPT
    assert f'<h1>{html.escape(title)}</h1>' in page
    assert f'<title>Установить {html.escape(title)}</title>' in page
    assert f'>Установить {html.escape(title)}</a>' in page


def test_install_landing_authorizes_only_its_static_script_and_preserves_download_headers(configured):
    cfg, fake, app, client = configured
    _, _, _, path = grant(configured)
    page = client.get(path)
    assert page.text.count('<script>') == page.text.count('</script>') == 1
    script = page.text.split('<script>')[1].split('</script>')[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    policy = page.headers['content-security-policy']
    script_directive = next(part.strip() for part in policy.split(';') if part.strip().startswith('script-src '))
    assert script_directive == f"script-src 'sha256-{digest}'"
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "base-uri 'none'" in policy
    assert 'id="install-app" href="itms-services://' in page.text
    assert path.rsplit('/', 1)[-1] not in script and cfg.public_base_url not in script
    head = client.head(path)
    assert head.status_code == 200 and head.content == b''
    assert head.headers['content-security-policy'] == policy
    assert int(head.headers['content-length']) == len(page.content)
    for suffix in ['/manifest.plist', '/app.ipa']:
        response = client.get(path+suffix)
        assert 'script-src' not in response.headers['content-security-policy']
        assert 'no-store' in response.headers['cache-control']
        assert response.headers['referrer-policy'] == 'no-referrer'


def test_install_landing_script_attempts_once_only_on_ios_and_keeps_fallback(configured):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is optional for backend-only environments; required for the frontend project.')
    _, _, _, client = configured
    _, _, _, path = grant(configured)
    page = client.get(path)
    script = page.text.split('<script>')[1].split('</script>')[0]
    scenarios = [
        {'userAgent': 'iPhone', 'platform': 'iPhone', 'maxTouchPoints': 5},
        {'userAgent': 'iPad', 'platform': 'iPad', 'maxTouchPoints': 5},
        {'userAgent': 'Macintosh', 'platform': 'MacIntel', 'maxTouchPoints': 5},
        {'userAgent': 'Macintosh', 'platform': 'MacIntel', 'maxTouchPoints': 0},
        {'userAgent': 'Android', 'platform': 'Linux', 'maxTouchPoints': 5},
        {'userAgent': 'Windows', 'platform': 'Win32', 'maxTouchPoints': 0},
        {'userAgent': 'iPhone', 'platform': 'iPhone', 'maxTouchPoints': 5, 'blocked': True},
        {'userAgent': 'iPhone', 'platform': 'iPhone', 'maxTouchPoints': 5, 'missingButton': True},
    ]
    runner = '''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const output = input.scenarios.map(scenario => {
  const calls = [], lookups = [];
  const button = { href: 'itms-services://?action=download-manifest&url=expected' };
  const context = {
    navigator: scenario,
    document: { getElementById(id) { lookups.push(id); return scenario.missingButton ? null : button; } },
    window: { location: { assign(href) { calls.push(href); if (scenario.blocked) throw new Error('blocked'); } } }
  };
  vm.runInNewContext(input.script, context, { timeout: 1000 });
  return { calls, lookups, href: button.href };
});
process.stdout.write(JSON.stringify(output));
'''
    result = subprocess.run([node, '-e', runner], input=json.dumps({'script': script, 'scenarios': scenarios}),
                            text=True, capture_output=True, check=True, timeout=10)
    outcomes = json.loads(result.stdout)
    for index, outcome in enumerate(outcomes):
        assert len(outcome['calls']) == (1 if index in (0, 1, 2, 6) else 0)
        assert outcome['lookups'] == (['install-app'] if index in (0, 1, 2, 6, 7) else [])
        assert all(href == outcome['href'] for href in outcome['calls'])
    assert 'id="install-app" href="itms-services://' in page.text


@pytest.mark.parametrize('recovery', ['same_key_retry', 'metadata_webhook'])
def test_timeout_after_remote_creation_recovers_the_original_payment(configured, recovery):
    cfg, fake, app, client = configured
    headers = login(client)
    payload = {'app_slugs': ['happ'], 'request_id': 'remote-created-timeout', 'expected_amount_rub': '1.00'}
    fake.fail_after_create = True
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 503
    draft = client.get('/api/orders', headers=headers).json()['items'][0]
    assert not draft['has_payment'] and draft['can_retry_payment'] and not draft['requires_payment_review']
    assert len(fake.values) == 1
    payment_id = next(iter(fake.values))
    if recovery == 'same_key_retry':
        recovered = client.post('/api/orders', headers=headers, json=payload)
        assert recovered.status_code == 200
        result = recovered.json()
        assert result['has_payment'] and not result['can_retry_payment'] and result['payment_url']
    else:
        fake.values[payment_id].update(status='succeeded', paid=True)
        event = {'event': 'payment.succeeded', 'object': {'id': payment_id, 'metadata': {'order_id': draft['id']}}}
        assert client.post('/api/payments/yookassa/webhook', json=event).status_code == 200
        result = client.get(f"/api/orders/{draft['id']}", headers=headers).json()
        assert result['status'] == 'succeeded' and result['has_payment'] and result['payment_url'] is None
    assert result['id'] == draft['id'] and len(fake.values) == 1 and len(set(fake.keys)) == 1
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1


def test_aged_unknown_checkout_cannot_be_bypassed_with_a_new_key(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    payload = {'app_slugs': ['happ'], 'request_id': 'aged-unknown', 'expected_amount_rub': '1.00'}
    fake.fail_create = True
    assert client.post('/api/orders', headers=headers, json=payload).status_code == 503
    draft = client.get('/api/orders', headers=headers).json()['items'][0]
    with app.state.db.connection(write=True) as conn:
        conn.execute('UPDATE orders SET created_at=? WHERE id=?', (int(time.time())-209*3600, draft['id']))
    calls = len(fake.keys)
    for request_id in ['aged-unknown', 'replacement-new-key']:
        response = client.post('/api/orders', headers=headers, json={**payload, 'request_id': request_id})
        assert response.status_code == 200
        review = response.json()
        assert review['id'] == draft['id'] and review['requires_payment_review']
        assert not review['has_payment'] and not review['can_retry_payment'] and review['payment_url'] is None
    assert client.post(f"/api/orders/{draft['id']}/retry-payment", headers=headers).status_code == 409
    cfg.happ_price_rub = '2.00'
    changed = {**payload, 'request_id': 'different-price-key', 'expected_amount_rub': '2.00'}
    assert client.post('/api/orders', headers=headers, json=changed).status_code == 409
    assert len(fake.keys) == calls
    cfg.yookassa_mode = 'live'
    other_mode = client.get(f"/api/orders/{draft['id']}", headers=headers).json()
    assert not other_mode['can_retry_payment'] and not other_mode['requires_payment_review']
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1


def test_known_payment_retry_reconciles_and_hides_canceled_url(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    original = order(client, headers)
    payment_id = next(iter(fake.values))
    assert original['has_payment'] and not original['can_retry_payment'] and original['payment_url']
    fake.fail_get = True
    path = f"/api/orders/{original['id']}/retry-payment"
    assert client.post(path, headers=headers).status_code == 503
    assert len(fake.keys) == 1
    fake.fail_get = False
    fake.values[payment_id].update(status='canceled', paid=False)
    canceled = client.post(path, headers=headers).json()
    assert canceled['status'] == 'canceled' and canceled['payment_url'] is None
    assert canceled['has_payment'] and not canceled['can_retry_payment'] and not canceled['requires_payment_review']
    assert order(client, headers)['payment_url'] is None
    assert len(fake.keys) == 1
    replacement = order(client, headers, 'confirmed-canceled-replacement')
    assert replacement['id'] != original['id'] and replacement['payment_url'] and len(fake.keys) == 2


@pytest.mark.parametrize('completed_cart', ['same_cart', 'partial_overlap', 'new_price'])
def test_new_key_reconciles_a_just_paid_draft_before_starting_checkout(configured, completed_cart):
    cfg, fake, app, client = configured
    headers = login(client)
    original = order(client, headers)
    payment_id = next(iter(fake.values))
    expected = '1.00'
    if completed_cart == 'partial_overlap':
        with app.state.db.connection(write=True) as conn:
            conn.execute('INSERT INTO order_items (order_id,app_slug,app_title,amount_rub) VALUES (?,?,?,?)',
                         (original['id'], 'historical-app', 'Historical fixture', '2.00'))
            conn.execute('UPDATE orders SET amount_rub=? WHERE id=?', ('3.00', original['id']))
        fake.values[payment_id]['amount']['value'] = '3.00'
    elif completed_cart == 'new_price':
        cfg.happ_price_rub = expected = '2.00'
    fake.values[payment_id].update(status='succeeded', paid=True)
    response = client.post('/api/orders', headers=headers, json={
        'app_slugs': ['happ'], 'request_id': 'new-key-after-confirmation', 'expected_amount_rub': expected})
    if completed_cart == 'same_cart':
        assert response.status_code == 200 and response.json()['id'] == original['id']
        assert response.json()['status'] == 'succeeded' and response.json()['payment_url'] is None
    else:
        assert response.status_code == 409
    assert len(fake.keys) == 1 and len(fake.gets) == 1
    assert client.get(f"/api/orders/{original['id']}", headers=headers).json()['status'] == 'succeeded'
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1


def test_concurrent_new_checkout_keys_reuse_one_pending_order(configured):
    from concurrent.futures import ThreadPoolExecutor
    cfg, fake, app, client = configured
    headers = login(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: order(client, headers, f'concurrent-new-key-{index}'), range(4)))
    assert len({result['id'] for result in results}) == 1
    assert len(set(fake.keys)) == 1 and len(fake.values) == 1
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 1


def test_multiple_legacy_overlaps_require_review_instead_of_another_payment(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    fake.fail_create = True
    assert client.post('/api/orders', headers=headers,
                       json={'app_slug': 'happ', 'request_id': 'first-legacy-draft'}).status_code == 503
    original = client.get('/api/orders', headers=headers).json()['items'][0]
    legacy_id = str(uuid.uuid4())
    with app.state.db.connection(write=True) as conn:
        conn.execute('''INSERT INTO orders (id,user_id,app_slug,app_title,amount_rub,request_id,created_at,test)
            VALUES (?,111,'happ','Legacy overlapping cart','3.00','second-legacy-draft',?,1)''',
                     (legacy_id, int(time.time())-209*3600))
        conn.executemany('INSERT INTO order_items (order_id,app_slug,app_title,amount_rub) VALUES (?,?,?,?)',
                         [(legacy_id, 'happ', 'Happ', '1.00'),
                          (legacy_id, 'historical-app', 'Historical fixture', '2.00')])
    calls = len(fake.keys)
    fake.fail_create = False
    response = client.post('/api/orders', headers=headers, json={
        'app_slugs': ['happ'], 'request_id': 'third-overlap-key', 'expected_amount_rub': '1.00'})
    assert response.status_code == 409 and len(fake.keys) == calls
    assert client.post('/api/orders', headers=headers, json={
        'app_slugs': ['happ'], 'request_id': 'first-legacy-draft', 'expected_amount_rub': '1.00'}).status_code == 409
    assert client.post(f"/api/orders/{original['id']}/retry-payment", headers=headers).status_code == 409
    assert len(fake.keys) == calls
    assert client.get(f"/api/orders/{original['id']}", headers=headers).json()['status'] == 'pending'


def test_paid_order_filter_is_validated_and_not_displaced_by_hidden_drafts(configured):
    cfg, fake, app, client = configured
    headers, purchased, _ = paid(configured)
    outsider = login(client, 222)
    with app.state.db.connection(write=True) as conn:
        for index in range(101):
            draft_id = str(uuid.uuid4())
            conn.execute('''INSERT INTO orders (id,user_id,app_slug,app_title,amount_rub,request_id,created_at,test)
                VALUES (?,111,'happ','Happ','1.00',?,?,1)''',
                         (draft_id, f'hidden-draft-{index}', int(time.time())+index+1))
            conn.execute('INSERT INTO order_items (order_id,app_slug,app_title,amount_rub) VALUES (?,?,?,?)',
                         (draft_id, 'happ', 'Happ', '1.00'))
    unfiltered = client.get('/api/orders', headers=headers).json()['items']
    assert len(unfiltered) == 100 and all(item['id'] != purchased['id'] for item in unfiltered)
    filtered = client.get('/api/orders?status=succeeded', headers=headers).json()['items']
    assert [item['id'] for item in filtered] == [purchased['id']]
    assert len(client.get('/api/orders?status=pending', headers=headers).json()['items']) == 100
    assert client.get('/api/orders?status=canceled', headers=headers).json()['items'] == []
    assert client.get('/api/orders?status=succeeded', headers=outsider).json()['items'] == []
    assert client.get('/api/orders?status=unknown', headers=headers).status_code == 422


def test_default_catalog_lists_seven_products_with_unpriced_planned_apps(configured):
    cfg, fake, app, client = configured
    items = client.get('/api/apps').json()['items']
    assert [(item['slug'], item['title']) for item in items] == [
        ('happ', 'Happ'), ('v2raytun', 'V2RayTun'), ('sber', 'Сбер'), ('tbank', 'Т-Банк'),
        ('vtb', 'ВТБ-Банк'), ('chatgpt', 'ChatGPT'), ('deepseek', 'DeepSeek')]
    assert [item['slug'] for item in items if item['available']] == ['happ']
    assert items[0]['price_rub'] == cfg.happ_price_rub and not items[0]['coming_soon']
    for item in items[1:]:
        assert item['coming_soon'] and not item['available'] and item['price_rub'] is None
        assert item['version'] == item['minimum_ios'] == '' and item['size_bytes'] == 0
        assert client.get(f"/api/apps/{item['slug']}").json() == item
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM apps').fetchone()[0] == 7
    assert fake.keys == [] and fake.gets == []


def test_default_price_is_490_in_catalog_without_operator_env(configured, tmp_path):
    fixture_cfg = configured[0]
    # The fixture cleared settings environment variables; explicitly disable .env
    # and omit the price override to exercise the deployment-independent default.
    cfg = Settings(_env_file=None, database_path=tmp_path/'default-price.sqlite3',
                   static_dir=tmp_path/'default-dist', happ_ipa_path=fixture_cfg.happ_ipa_path,
                   happ_ipa_sha256=fixture_cfg.happ_ipa_sha256)
    assert cfg.happ_price_rub == '490.00'
    with TestClient(create_app(cfg)) as client:
        items = client.get('/api/apps').json()['items']
        happ = next(item for item in items if item['slug'] == 'happ')
        assert happ['available'] and happ['price_rub'] == '490.00'
        assert all(item['price_rub'] is None for item in items if item['coming_soon'])


def test_planned_and_mixed_carts_fail_before_saving_orders_or_creating_payments(configured):
    cfg, fake, app, client = configured
    headers = login(client)
    slugs = ['v2raytun', 'sber', 'tbank', 'vtb', 'chatgpt', 'deepseek']
    for slug in slugs:
        for selection in [[slug], ['happ', slug]]:
            response = client.post('/api/orders', headers=headers, json={
                'app_slugs': selection, 'request_id': f'planned-cart-{slug}-{len(selection)}'})
            assert response.status_code == 503
            assert response.json()['detail'] == 'Это приложение готовится к запуску. Оплата пока недоступна.'
    assert client.post('/api/orders', headers=headers,
                       json={'app_slug': 'chatgpt', 'request_id': 'legacy-planned-cart'}).status_code == 503
    assert fake.keys == [] and fake.values == {}
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM order_items').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM install_grants').fetchone()[0] == 0


def test_planned_listing_cannot_serve_installation_even_if_referenced_by_a_paid_record(configured):
    cfg, fake, app, client = configured
    owner, purchased, _, path = grant(configured)
    # Simulate a legacy record that references a currently planned product. Its old
    # grant must not fall back to Happ's file or create a new capability.
    with app.state.db.connection(write=True) as conn:
        conn.execute('UPDATE order_items SET app_slug=?,app_title=? WHERE order_id=?',
                     ('chatgpt', 'ChatGPT', purchased['id']))
        conn.execute('UPDATE install_grants SET app_slug=? WHERE order_id=?', ('chatgpt', purchased['id']))
    base = f"/api/orders/{purchased['id']}/items/chatgpt"
    result = client.get(f"/api/orders/{purchased['id']}", headers=owner).json()
    assert not result['can_install'] and not result['items'][0]['can_install']
    assert client.post(base+'/install-link', headers=owner).status_code == 503
    assert client.get(base+'/activation', headers=owner).status_code == 503
    for suffix in ['', '/manifest.plist', '/app.ipa']:
        assert client.get(path+suffix).status_code == 503
    with app.state.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM install_grants').fetchone()[0] == 1
    assert len(fake.keys) == 1
