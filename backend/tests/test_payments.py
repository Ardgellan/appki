import base64
import copy
import json

import httpx
import pytest

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


def client_for(handler):
    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YooKassaClient(SHOP_ID, SECRET, client=injected), injected


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
async def test_create_sends_official_authenticated_request_and_reuses_exact_key():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payment())

    client, transport_client = client_for(handle)
    async with transport_client:
        first = await client.create_payment(**CREATE_ARGS)
        second = await client.create_payment(**CREATE_ARGS)
    assert first == second == payment()
    assert len(requests) == 2
    expected_auth = "Basic " + base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()
    for request in requests:
        assert str(request.url) == API_BASE + "/payments"
        assert request.method == "POST"
        assert request.headers["Authorization"] == expected_auth
        assert request.headers["Idempotence-Key"] == "purchase-123"
        assert json.loads(request.content) == {
            "amount": {"value": AMOUNT, "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": CREATE_ARGS["return_url"]},
            "description": CREATE_ARGS["description"],
            "metadata": {"order_id": ORDER_ID},
        }


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
