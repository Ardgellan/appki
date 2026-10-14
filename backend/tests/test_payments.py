import asyncio
import base64
import copy
import json
import socket

import httpx
import pytest

import appki.payments as payments_module
from appki.payments import (
    API_BASE,
    PaymentConfigurationError,
    PaymentProviderError,
    PaymentValidationError,
    YooKassaClient,
    validate_payment,
)


PAYMENT_ID = "23d93cac-000f-5000-8000-126628f15141"
OTHER_ID = "23d93cac-000f-5000-8000-126628f15142"
SHOP_ID = "100500"
SECRET = "test_not_a_real_key"
ORDER_ID = "order-123"
AMOUNT = "199.90"
CREATE_ARGS = {
    "order_id": ORDER_ID,
    "amount_rub": AMOUNT,
    "description": "Доступ к установке Happ",
    "return_url": "https://apps.example/payment/return?order_id=order-123",
    "idempotence_key": "purchase-123",
}


def payment(status="pending", **updates):
    result = {
        "id": PAYMENT_ID,
        "status": status,
        "paid": status in ("succeeded", "waiting_for_capture"),
        "amount": {"value": AMOUNT, "currency": "RUB"},
        "metadata": {"order_id": ORDER_ID},
        "test": True,
        "recipient": {"account_id": SHOP_ID, "gateway_id": "100700"},
    }
    if status == "pending":
        result["confirmation"] = {
            "type": "redirect",
            "confirmation_url": "https://yoomoney.ru/api-pages/v2/payment-confirm/epl?orderId=" + PAYMENT_ID,
        }
    result.update(updates)
    return result


def validate(value, **kwargs):
    options = {
        "order_id": ORDER_ID,
        "payment_id": PAYMENT_ID,
        "amount_rub": AMOUNT,
        "shop_id": SHOP_ID,
        "mode": "test",
    }
    options.update(kwargs)
    return validate_payment(value, **options)


def client_for(handler, *, mode="test"):
    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YooKassaClient(SHOP_ID, mode + "_not_a_real_key", mode=mode, client=injected), injected


@pytest.mark.parametrize("status,result", [("pending", "pending"), ("succeeded", "succeeded"), ("canceled", "canceled"), ("waiting_for_capture", "pending")])
def test_authoritative_payment_states(status, result):
    assert validate(payment(status)) == result


def test_live_payment_needs_live_mode_and_bool_test_flag():
    assert validate(payment("succeeded", test=False), mode="live") == "succeeded"
    with pytest.raises(PaymentValidationError):
        validate(payment("succeeded", test=True), mode="live")


@pytest.mark.parametrize("updates", [
    {"id": OTHER_ID},
    {"id": "../../refunds"},
    {"id": None},
    {"metadata": {"order_id": "somebody-elses-order"}},
    {"metadata": {}},
    {"metadata": None},
    {"amount": {"value": "199.89", "currency": "RUB"}},
    {"amount": {"value": "199.90", "currency": "USD"}},
    {"amount": {"value": 199.90, "currency": "RUB"}},
    {"amount": {"value": "199.900", "currency": "RUB"}},
    {"amount": None},
    {"test": False},
    {"test": "true"},
    {"test": 1},
    {"test": None},
    {"paid": False},
    {"paid": 1},
    {"paid": "true"},
    {"recipient": {"account_id": "99999"}},
    {"recipient": {"account_id": int(SHOP_ID)}},
    {"recipient": {}},
    {"recipient": None},
    {"status": "refunded"},
    {"status": []},
    {"captured_amount": {"value": "100.00", "currency": "RUB"}},
    {"captured_amount": {"value": AMOUNT, "currency": "USD"}},
    {"captured_amount": None},
    {"refunded_amount": {"value": "200.00", "currency": "RUB"}},
    {"refunded_amount": {"value": "-1.00", "currency": "RUB"}},
    {"refunded_amount": {"value": "10.00", "currency": "USD"}},
    {"refunded_amount": None},
])
def test_mismatched_or_malformed_response_never_confirms_payment(updates):
    with pytest.raises(PaymentValidationError):
        validate({**payment("succeeded"), **updates})


def test_optional_recipient_and_captured_amount_and_validated_full_refund():
    value = payment("succeeded", captured_amount={"value": AMOUNT, "currency": "RUB"})
    del value["recipient"]
    assert validate(value) == "succeeded"
    value["refunded_amount"] = {"value": AMOUNT, "currency": "RUB"}
    # Backend can now revoke the entitlement based on this verified amount.
    assert validate(value) == "succeeded"
    assert value["refunded_amount"]["value"] == AMOUNT


@pytest.mark.parametrize("amount", ["0.00", "-1.00", "1", "1.0", "01.00", "NaN", "Infinity", "1e2", 1.0, None, "10000000000.00"])
def test_expected_amount_rejects_float_rounding_and_noncanonical_values(amount):
    with pytest.raises(PaymentValidationError):
        validate(payment(), amount_rub=amount)


@pytest.mark.parametrize("options", [
    {"shop_id": ""},
    {"shop_id": None},
    {"shop_id": "user:password"},
    {"secret_key": ""},
    {"secret_key": None},
    {"secret_key": "live_wrong_mode"},
    {"secret_key": "test_"},
    {"secret_key": "test_line\nbreak"},
    {"mode": "live"},
    {"mode": "sandbox"},
    {"api_base": "http://api.yookassa.ru/v3"},
    {"api_base": "https://evil.example/v3"},
    {"api_base": "https://api.yookassa.ru.evil.example/v3"},
])
def test_bad_configuration_fails_before_any_request(options):
    args = {"shop_id": SHOP_ID, "secret_key": SECRET}
    args.update(options)
    with pytest.raises(PaymentConfigurationError) as error:
        YooKassaClient(**args)
    assert SECRET not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,payment_method_data", [("test", None), ("live", {"type": "sbp"})])
async def test_create_uses_sbp_only_live_and_reuses_exact_key(mode, payment_method_data):
    requests = []
    provider_payment = payment(test=mode == "test")
    if mode == "live":
        provider_payment["payment_method"] = {"type": "sbp", "id": PAYMENT_ID, "saved": False}
        provider_payment["confirmation"]["confirmation_url"] = "https://yoomoney.ru/checkout/payments/sbp?orderId=" + PAYMENT_ID

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=provider_payment)

    client, transport_client = client_for(handle, mode=mode)
    async with transport_client:
        first = await client.create_payment(**CREATE_ARGS)
        second = await client.create_payment(**CREATE_ARGS)
    assert first == second == provider_payment
    assert len(requests) == 2
    expected_auth = "Basic " + base64.b64encode(f"{SHOP_ID}:{mode}_not_a_real_key".encode()).decode()
    expected_payload = {
        "amount": {"value": AMOUNT, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": CREATE_ARGS["return_url"]},
        "description": CREATE_ARGS["description"],
        "metadata": {"order_id": ORDER_ID},
    }
    if payment_method_data is not None:
        expected_payload["payment_method_data"] = payment_method_data
    for request in requests:
        assert str(request.url) == API_BASE + "/payments"
        assert request.method == "POST"
        assert request.headers["Authorization"] == expected_auth
        assert request.headers["Idempotence-Key"] == "purchase-123"
        assert json.loads(request.content) == expected_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"idempotence_key": "x\r\nInjected: yes"},
    {"idempotence_key": "a" * 65},
    {"order_id": "../x"},
    {"description": "a" * 129},
    {"return_url": "http://apps.example/return"},
    {"return_url": "https://user:password@apps.example/"},
    {"return_url": "https://apps.example\\@evil.example/"},
    {"return_url": "javascript:alert(1)"},
    {"amount_rub": 199.9},
])
async def test_invalid_creation_inputs_make_no_network_request(updates):
    requests = []
    client, transport_client = client_for(lambda request: requests.append(request))
    async with transport_client:
        with pytest.raises(PaymentValidationError):
            await client.create_payment(**{**CREATE_ARGS, **updates})
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [None, {}, {"type": "external"}, {"type": "redirect", "confirmation_url": "http://yoomoney.ru/"}, {"type": "redirect", "confirmation_url": "javascript:alert(1)"}])
async def test_create_rejects_unusable_confirmation(confirmation):
    client, transport_client = client_for(lambda request: httpx.Response(200, json=payment(confirmation=confirmation)))
    async with transport_client:
        with pytest.raises(PaymentValidationError):
            await client.create_payment(**CREATE_ARGS)


@pytest.mark.asyncio
async def test_creation_retry_can_return_already_succeeded_payment():
    client, transport_client = client_for(lambda request: httpx.Response(200, json=payment("succeeded")))
    async with transport_client:
        assert (await client.create_payment(**CREATE_ARGS))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_create_also_validates_authoritative_order_identity():
    bad = payment(metadata={"order_id": "another-order"})
    client, transport_client = client_for(lambda request: httpx.Response(200, json=bad))
    async with transport_client:
        with pytest.raises(PaymentValidationError):
            await client.create_payment(**CREATE_ARGS)


@pytest.mark.asyncio
async def test_get_payment_uses_fixed_origin_even_with_injected_base_url():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payment("succeeded"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://evil.example") as injected:
        client = YooKassaClient(SHOP_ID, SECRET, client=injected)
        assert (await client.get_payment(PAYMENT_ID))["id"] == PAYMENT_ID
    assert str(requests[0].url) == API_BASE + "/payments/" + PAYMENT_ID
    assert requests[0].method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "../refunds", "https://evil.example/", "//evil.example/", PAYMENT_ID + "?x=1", PAYMENT_ID + "/capture", PAYMENT_ID + "%2f", "a" * 10000, None])
async def test_get_rejects_path_injection_before_network(value):
    requests = []
    client, transport_client = client_for(lambda request: requests.append(request))
    async with transport_client:
        with pytest.raises(PaymentValidationError):
            await client.get_payment(value)
    assert requests == []


@pytest.mark.asyncio
async def test_get_rejects_mismatched_payment_id():
    client, transport_client = client_for(lambda request: httpx.Response(200, json=payment(id=OTHER_ID)))
    async with transport_client:
        with pytest.raises(PaymentValidationError):
            await client.get_payment(PAYMENT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>bad gateway</html>"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json="not a payment"),
    httpx.Response(200, json=None),
    httpx.Response(400, json={"description": SECRET}),
    httpx.Response(429, json={"description": SECRET}),
    httpx.Response(500, json={"description": SECRET}),
    httpx.Response(302, headers={"Location": "https://evil.example/"}),
])
async def test_provider_errors_do_not_leak_payload_or_follow_redirect(response):
    requests = []

    def handler(request):
        requests.append(request)
        return copy.copy(response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as injected:
        client = YooKassaClient(SHOP_ID, SECRET, client=injected)
        with pytest.raises(PaymentProviderError) as error:
            await client.get_payment(PAYMENT_ID)
        assert SECRET not in str(error.value)
        assert "evil.example" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_are_safe_configuration_errors(status):
    client, transport_client = client_for(lambda request: httpx.Response(status, json={"secret": SECRET}))
    async with transport_client:
        with pytest.raises(PaymentConfigurationError) as error:
            await client.get_payment(PAYMENT_ID)
        assert SECRET not in str(error.value)


@pytest.mark.asyncio
async def test_network_failure_is_safe_and_no_automatic_new_payment_is_created():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("transport contained " + SECRET, request=request)

    client, transport_client = client_for(handler)
    async with transport_client:
        with pytest.raises(PaymentProviderError) as error:
            await client.create_payment(**CREATE_ARGS)
        assert SECRET not in str(error.value)
        assert error.value.__suppress_context__ is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_client_closes_owned_transport_only():
    own = YooKassaClient(SHOP_ID, SECRET)
    await own.close()
    await own.close()
    assert own._client.is_closed
    client, transport_client = client_for(lambda request: httpx.Response(200, json=payment()))
    try:
        await client.close()
        assert not transport_client.is_closed
    finally:
        await transport_client.aclose()


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []
        self.closed = False

    async def handle_async_request(self, request):
        self.requests.append({
            'url': request.url, 'method': request.method, 'headers': request.headers.copy(),
            'body': await request.aread(), 'extensions': dict(request.extensions),
        })
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self):
        self.closed = True


def patch_addresses(monkeypatch, addresses):
    calls = []

    async def resolve():
        calls.append(True)
        return addresses

    monkeypatch.setattr(payments_module, '_resolve_yookassa_addresses', resolve)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [httpx.ConnectError, httpx.ConnectTimeout])
async def test_dns_fallback_preserves_payment_and_official_tls_identity(monkeypatch, failure):
    dns_calls = patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1'])
    inner = RecordingTransport([failure('TCP/TLS unavailable'), httpx.Response(200)])
    transport = payments_module._YooKassaTransport(inner)
    body = b'{"amount":{"value":"490.00","currency":"RUB"}}'
    request = httpx.Request(
        'POST', API_BASE + '/payments?reference=a%20b', content=body,
        headers={'Authorization': 'Basic test-auth', 'Idempotence-Key': 'same-order-key',
                 'Content-Type': 'application/json'},
        extensions={'timeout': {'connect': 5.0, 'read': 20.0}, 'marker': 'preserved'},
    )
    response = await transport.handle_async_request(request)
    assert response.status_code == 200 and len(dns_calls) == 1
    original, fallback = inner.requests
    assert original['url'].host == 'api.yookassa.ru'
    assert fallback['url'].host == '1.1.1.1' and fallback['url'].scheme == 'https'
    assert fallback['url'].raw_path == request.url.raw_path
    assert fallback['headers']['Host'] == 'api.yookassa.ru'
    assert fallback['extensions']['sni_hostname'] == 'api.yookassa.ru'
    assert fallback['extensions']['timeout'] == original['extensions']['timeout']
    assert fallback['extensions']['marker'] == 'preserved'
    assert fallback['method'] == original['method'] == 'POST'
    assert fallback['headers'] == original['headers']
    assert fallback['body'] == original['body'] == body
    assert request.url.host == 'api.yookassa.ru' and 'sni_hostname' not in request.extensions
    await transport.aclose()
    assert inner.closed


@pytest.mark.asyncio
async def test_dns_fallback_routes_ipv6_with_original_hostname(monkeypatch):
    patch_addresses(monkeypatch, ['8.8.8.8', '2606:4700:4700::1111'])
    inner = RecordingTransport([httpx.ConnectTimeout('TLS unavailable'), httpx.Response(200)])
    response = await payments_module._YooKassaTransport(inner).handle_async_request(
        httpx.Request('GET', API_BASE + '/payments/' + PAYMENT_ID))
    assert response.status_code == 200
    routed = inner.requests[1]
    assert routed['url'].host == '2606:4700:4700::1111'
    assert routed['headers']['Host'] == routed['extensions']['sni_hostname'] == 'api.yookassa.ru'


@pytest.mark.asyncio
async def test_dns_fallback_all_connections_fail_without_leaking_or_changing_key(monkeypatch):
    dns_calls = patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1'])
    inner = RecordingTransport([httpx.ConnectTimeout(SECRET), httpx.ConnectError(SECRET),
                                httpx.ConnectTimeout(SECRET)])
    async with httpx.AsyncClient(transport=payments_module._YooKassaTransport(inner)) as injected:
        client = YooKassaClient(SHOP_ID, SECRET, client=injected)
        with pytest.raises(PaymentProviderError) as error:
            await client.create_payment(**CREATE_ARGS)
    assert SECRET not in str(error.value) and error.value.__suppress_context__
    assert len(inner.requests) == 3 and len(dns_calls) == 1
    assert {request['headers']['Idempotence-Key'] for request in inner.requests} == {'purchase-123'}
    assert len({request['body'] for request in inner.requests}) == 1
    assert inner.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://other.example/v3/payments', 'http://api.yookassa.ru/v3/payments',
                               'https://api.yookassa.ru:8443/v3/payments'])
async def test_dns_fallback_never_applies_to_other_origins(monkeypatch, url):
    dns_calls = patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1'])
    inner = RecordingTransport([httpx.ConnectTimeout('unavailable')])
    with pytest.raises(httpx.ConnectTimeout):
        await payments_module._YooKassaTransport(inner).handle_async_request(httpx.Request('GET', url))
    assert len(inner.requests) == 1 and dns_calls == []


@pytest.mark.asyncio
async def test_dns_fallback_does_not_retry_without_public_addresses(monkeypatch):
    dns_calls = patch_addresses(monkeypatch, [])
    inner = RecordingTransport([httpx.ConnectTimeout('unavailable')])
    with pytest.raises(httpx.ConnectTimeout):
        await payments_module._YooKassaTransport(inner).handle_async_request(httpx.Request('GET', API_BASE))
    assert len(inner.requests) == 1 and len(dns_calls) == 1


@pytest.mark.asyncio
async def test_dns_fallback_can_recover_with_a_single_fresh_address(monkeypatch):
    dns_calls = patch_addresses(monkeypatch, ['8.8.8.8'])
    inner = RecordingTransport([httpx.ConnectTimeout('old DNS route failed'), httpx.Response(200)])
    request = httpx.Request('POST', API_BASE + '/payments', content=b'unchanged-payment',
                           headers={'Authorization': 'Basic test-auth', 'Idempotence-Key': 'same-key'})
    response = await payments_module._YooKassaTransport(inner).handle_async_request(request)
    assert response.status_code == 200 and len(dns_calls) == 1
    original, direct = inner.requests
    assert direct['url'].host == '8.8.8.8' and direct['url'].scheme == 'https'
    assert direct['headers'] == original['headers']
    assert direct['headers']['Host'] == direct['extensions']['sni_hostname'] == 'api.yookassa.ru'
    assert direct['body'] == original['body'] == b'unchanged-payment'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout,
                                   httpx.ReadError, httpx.PoolTimeout])
async def test_dns_fallback_never_retries_uncertain_http_or_pool_failures(monkeypatch, failure):
    dns_calls = patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1'])
    inner = RecordingTransport([failure('do not replay HTTP')])
    with pytest.raises(failure):
        await payments_module._YooKassaTransport(inner).handle_async_request(httpx.Request('POST', API_BASE))
    assert len(inner.requests) == 1 and dns_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [200, 302, 400, 401, 403, 429, 500])
async def test_dns_fallback_never_retries_an_http_response(monkeypatch, status):
    dns_calls = patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1'])
    inner = RecordingTransport([httpx.Response(status)])
    response = await payments_module._YooKassaTransport(inner).handle_async_request(
        httpx.Request('POST', API_BASE))
    assert response.status_code == status and len(inner.requests) == 1 and dns_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [httpx.ReadTimeout('response unknown'),
                                   httpx.WriteError('request may be sent'),
                                   httpx.Response(400), httpx.Response(302)])
async def test_dns_fallback_stops_after_an_alternate_reaches_http(monkeypatch, outcome):
    patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1', '9.9.9.9'])
    inner = RecordingTransport([httpx.ConnectTimeout('first route failed'), outcome])
    transport = payments_module._YooKassaTransport(inner)
    request = httpx.Request('POST', API_BASE + '/payments', content=b'same-payment',
                           headers={'Idempotence-Key': 'same-key'})
    if isinstance(outcome, Exception):
        with pytest.raises(type(outcome)):
            await transport.handle_async_request(request)
    else:
        assert (await transport.handle_async_request(request)).status_code == outcome.status_code
    assert [request['url'].host for request in inner.requests] == ['api.yookassa.ru', '1.1.1.1']


@pytest.mark.asyncio
async def test_dns_fallback_caps_alternate_connections(monkeypatch):
    patch_addresses(monkeypatch, ['8.8.8.8', '1.1.1.1', '9.9.9.9', '8.8.4.4', '1.0.0.1'])
    inner = RecordingTransport([httpx.ConnectTimeout('unavailable') for _ in range(4)])
    with pytest.raises(httpx.ConnectTimeout):
        await payments_module._YooKassaTransport(inner).handle_async_request(httpx.Request('GET', API_BASE))
    assert [request['url'].host for request in inner.requests] == [
        'api.yookassa.ru', '1.1.1.1', '9.9.9.9', '8.8.8.8']


@pytest.mark.asyncio
async def test_dns_resolution_is_official_bounded_and_filters_nonpublic_duplicates(monkeypatch):
    calls = []

    async def getaddrinfo(host, port, **kwargs):
        calls.append((host, port, kwargs))
        addresses = [(socket.AF_INET, '8.8.8.8'), (socket.AF_INET, '8.8.8.8'),
                     (socket.AF_INET, '127.0.0.1'), (socket.AF_INET, '10.0.0.1'),
                     (socket.AF_INET, '224.0.0.1'), (socket.AF_INET6, '::1'),
                     (socket.AF_INET6, '::ffff:8.8.8.8'),
                     (socket.AF_INET6, '2606:4700:4700::1111'),
                     (socket.AF_INET6, '2606:4700:4700::1111'),
                     (socket.AF_INET, 'not-an-address')]
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))
                for family, address in addresses]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', getaddrinfo)
    assert await payments_module._resolve_yookassa_addresses() == ['8.8.8.8', '2606:4700:4700::1111']
    assert calls == [('api.yookassa.ru', 443, {'type': socket.SOCK_STREAM, 'proto': socket.IPPROTO_TCP})]


@pytest.mark.asyncio
async def test_dns_deadline_keeps_original_connection_failure_without_retry(monkeypatch):
    async def getaddrinfo(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(payments_module, '_DNS_TIMEOUT_SECONDS', 0.001)
    inner = RecordingTransport([httpx.ConnectTimeout('original TLS failure')])
    with pytest.raises(httpx.ConnectTimeout, match='original TLS failure'):
        await payments_module._YooKassaTransport(inner).handle_async_request(httpx.Request('GET', API_BASE))
    assert len(inner.requests) == 1
