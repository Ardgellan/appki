"""Small server-side YooKassa adapter with explicit payment verification.

Protocol references: https://yookassa.ru/developers/using-api/interaction-format
and https://yookassa.ru/developers/payment-acceptance/getting-started/quick-start.
Never trust webhook payloads or the payment return URL as proof of payment.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx


API_BASE = "https://api.yookassa.ru/v3"
_API_HOST = "api.yookassa.ru"
_DNS_TIMEOUT_SECONDS = 5.0
_MAX_FALLBACK_ADDRESSES = 3
_PAYMENT_ID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
_AMOUNT = re.compile(r"(?:0|[1-9][0-9]{0,9})\.[0-9]{2}\Z")
_SAFE_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_SHOP_ID = re.compile(r"[0-9]{1,32}\Z")
_UNVERIFIED = "Не удалось подтвердить платёж. Доступ пока не выдан."


class PaymentConfigurationError(Exception):
    """Configuration is invalid or provider credentials were rejected.

    A rejected retry does not resolve an earlier uncertain creation attempt.
    """


class PaymentProviderError(Exception):
    """Provider request failed; its result can be unknown, so preserve the order."""


class PaymentValidationError(Exception):
    """An input or provider payment cannot be correlated safely with this order."""


async def _resolve_yookassa_addresses() -> list[str]:
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(
            _API_HOST, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        ),
        timeout=_DNS_TIMEOUT_SECONDS,
    )
    addresses = []
    for family, _, _, _, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or '%' in str(address):
            continue
        value = str(address)
        if value not in addresses:
            addresses.append(value)
    return addresses


class _YooKassaTransport(httpx.AsyncBaseTransport):
    """Try DNS alternatives only when TCP/TLS failed before sending HTTP.

    IP URLs are internal routing only. Host and TLS hostname verification remain
    bound to the official API; see https://www.python-httpx.org/advanced/extensions/.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._inner.handle_async_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if (request.url.scheme != 'https' or request.url.host != _API_HOST
                    or request.url.port not in (None, 443)):
                raise
            original_error = exc
        try:
            addresses = await _resolve_yookassa_addresses()
        except (OSError, TimeoutError):
            raise original_error from None
        # Fresh DNS ordering can differ from the failed hostname lookup. Retain
        # its first address too, but try the other bounded candidates before it.
        alternatives = addresses[:_MAX_FALLBACK_ADDRESSES]
        if len(alternatives) > 1:
            alternatives = alternatives[1:] + alternatives[:1]
        if not alternatives:
            raise original_error from None
        body = await request.aread()
        headers = request.headers.copy()
        headers['Host'] = _API_HOST
        for address in alternatives:
            routed = httpx.Request(
                request.method, request.url.copy_with(host=address), headers=headers,
                stream=httpx.ByteStream(body),
                extensions={**request.extensions, 'sni_hostname': _API_HOST},
            )
            try:
                return await self._inner.handle_async_request(routed)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
        raise original_error from None

    async def aclose(self) -> None:
        await self._inner.aclose()


def _money(value: Any, *, positive: bool = True) -> Decimal:
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value):
        raise PaymentValidationError(_UNVERIFIED)
    amount = Decimal(value)
    if positive and amount <= 0:
        raise PaymentValidationError(_UNVERIFIED)
    return amount


def _payment_id(value: Any) -> str:
    # The provider IDs are UUID-shaped, but not necessarily UUIDv4.
    if not isinstance(value, str) or not _PAYMENT_ID.fullmatch(value):
        raise PaymentValidationError(_UNVERIFIED)
    return value


def _https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
            and not url.fragment
        )
    except ValueError:
        return False


def validate_payment(
    payment: dict,
    *,
    order_id: str,
    payment_id: str | None,
    amount_rub: str,
    shop_id: str,
    mode: str,
) -> str:
    """Verify an authoritative API response before granting access.

    A caller must first obtain ``payment`` through authenticated YooKassa API.
    This routine alone does not authenticate arbitrary webhook JSON. Partial
    captures cannot buy a full entitlement. Full refunds are handled by callers
    from the validated ``refunded_amount`` field, never raw notification data.
    """
    if mode not in ("test", "live") or not isinstance(shop_id, str) or not _SHOP_ID.fullmatch(shop_id):
        raise PaymentConfigurationError("Неверные настройки магазина ЮKassa.")
    expected = _money(amount_rub)
    if not isinstance(order_id, str) or not _SAFE_ID.fullmatch(order_id):
        raise PaymentValidationError(_UNVERIFIED)
    if not isinstance(payment, dict):
        raise PaymentValidationError(_UNVERIFIED)
    actual_id = _payment_id(payment.get("id"))
    if payment_id is not None and actual_id != _payment_id(payment_id):
        raise PaymentValidationError(_UNVERIFIED)
    metadata = payment.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("order_id") != order_id:
        raise PaymentValidationError(_UNVERIFIED)
    amount = payment.get("amount")
    if not isinstance(amount, dict) or amount.get("currency") != "RUB" or _money(amount.get("value")) != expected:
        raise PaymentValidationError(_UNVERIFIED)
    if type(payment.get("test")) is not bool or payment["test"] != (mode == "test"):
        raise PaymentValidationError(_UNVERIFIED)
    if "recipient" in payment:
        recipient = payment["recipient"]
        if not isinstance(recipient, dict) or recipient.get("account_id") != shop_id:
            raise PaymentValidationError(_UNVERIFIED)
    status = payment.get("status")
    paid = payment.get("paid")
    if status not in ("pending", "waiting_for_capture", "succeeded", "canceled") or type(paid) is not bool:
        raise PaymentValidationError(_UNVERIFIED)
    if status == "succeeded" and paid is not True:
        raise PaymentValidationError(_UNVERIFIED)
    if "captured_amount" in payment:
        captured = payment["captured_amount"]
        if not isinstance(captured, dict) or captured.get("currency") != "RUB":
            raise PaymentValidationError(_UNVERIFIED)
        captured_value = _money(captured.get("value"), positive=False)
        if captured_value > expected or (status == "succeeded" and captured_value != expected):
            raise PaymentValidationError(_UNVERIFIED)
    if "refunded_amount" in payment:
        refunded = payment["refunded_amount"]
        if not isinstance(refunded, dict) or refunded.get("currency") != "RUB":
            raise PaymentValidationError(_UNVERIFIED)
        if _money(refunded.get("value"), positive=False) > expected:
            raise PaymentValidationError(_UNVERIFIED)
    # A hold is not a completed purchase, even when paid=true in YooKassa.
    return "pending" if status == "waiting_for_capture" else status


class YooKassaClient:
    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        mode: str = "test",
        api_base: str = API_BASE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if api_base != API_BASE:
            raise PaymentConfigurationError("Допустим только официальный API ЮKassa.")
        if (
            mode not in ("test", "live")
            or not isinstance(shop_id, str)
            or not _SHOP_ID.fullmatch(shop_id)
            or not isinstance(secret_key, str)
            or not secret_key.startswith(mode + "_")
            or len(secret_key) <= len(mode) + 1
            or len(secret_key) > 1024
            or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in secret_key)
        ):
            raise PaymentConfigurationError("Настройте shopId и ключ ЮKassa для выбранного режима.")
        self.shop_id = shop_id
        self.mode = mode
        self._auth = httpx.BasicAuth(shop_id, secret_key)
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            transport=_YooKassaTransport(),
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            # Absolute, fixed-origin URLs prevent base_url configuration or
            # injected clients from redirecting merchant credentials elsewhere.
            response = await self._client.request(
                method,
                API_BASE + path,
                auth=self._auth,
                follow_redirects=False,
                headers={"Accept": "application/json", **kwargs.pop("headers", {})},
                **kwargs,
            )
        except (httpx.HTTPError, RuntimeError):
            # Never include raw errors: they may contain URLs, request bodies,
            # headers or transport details. A retry must reuse the original key.
            raise PaymentProviderError("ЮKassa временно недоступна. Повторите проверку позже.") from None
        if response.status_code in (401, 403):
            raise PaymentConfigurationError("ЮKassa не приняла настройки магазина.")
        if response.status_code not in (200, 201):
            raise PaymentProviderError("ЮKassa не подтвердила запрос. Повторите проверку позже.")
        try:
            payment = response.json()
        except (ValueError, UnicodeError):
            raise PaymentProviderError("Получен некорректный ответ ЮKassa.") from None
        if not isinstance(payment, dict):
            raise PaymentProviderError("Получен некорректный ответ ЮKassa.")
        return payment

    async def create_payment(
        self,
        *,
        order_id: str,
        amount_rub: str,
        description: str,
        return_url: str,
        idempotence_key: str,
    ) -> dict:
        _money(amount_rub)
        if (
            not isinstance(order_id, str)
            or not _SAFE_ID.fullmatch(order_id)
            or not isinstance(idempotence_key, str)
            or not _SAFE_ID.fullmatch(idempotence_key)
            or not isinstance(description, str)
            or not 1 <= len(description) <= 128
            or any(ord(char) < 32 for char in description)
            or not _https_url(return_url)
        ):
            raise PaymentValidationError("Не удалось подготовить платёж. Проверьте параметры заказа.")
        payload = {
            "amount": {"value": amount_rub, "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description,
            "metadata": {"order_id": order_id},
        }
        # SBP is available only in real shops; test shops keep their test checkout.
        if self.mode == "live":
            payload["payment_method_data"] = {"type": "sbp"}
        payment = await self._request(
            "POST",
            "/payments",
            headers={"Idempotence-Key": idempotence_key},
            json=payload,
        )
        status = validate_payment(
            payment,
            order_id=order_id,
            payment_id=None,
            amount_rub=amount_rub,
            shop_id=self.shop_id,
            mode=self.mode,
        )
        confirmation = payment.get("confirmation")
        if confirmation is not None:
            if (
                not isinstance(confirmation, dict)
                or confirmation.get("type") != "redirect"
                or not _https_url(confirmation.get("confirmation_url"))
            ):
                raise PaymentValidationError(_UNVERIFIED)
        elif status == "pending" and payment["status"] != "waiting_for_capture":
            raise PaymentValidationError(_UNVERIFIED)
        return payment

    async def get_payment(self, payment_id: str) -> dict:
        _payment_id(payment_id)
        payment = await self._request("GET", "/payments/" + payment_id)
        # Correlate before returning, including before caller reads metadata.
        if _payment_id(payment.get("id")) != payment_id:
            raise PaymentValidationError(_UNVERIFIED)
        return payment

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()
