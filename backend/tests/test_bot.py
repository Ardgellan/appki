import httpx
import pytest

from appki.bot import TelegramClient, TelegramError, handle_update, read_offset, reply_for, save_offset
from appki.config import Settings


def settings():
    return Settings(_env_file=None, bot_token='123:fake', bot_username='appki_test_bot', public_base_url='https://example.com')


@pytest.mark.asyncio
async def test_worker_does_not_send_private_store_buttons_in_groups():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True, 'result': {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = TelegramClient('123:secret', transport)
        await handle_update(client, {'message': {'chat': {'id': -1, 'type': 'group'}, 'text': '/start'}}, settings())
        assert not requests
        await handle_update(client, {'message': {'chat': {'id': 1, 'type': 'private'}, 'text': '/purchases'}}, settings())
        assert len(requests) == 1
        import json
        payload = json.loads(requests[0].content)
        assert payload['reply_markup']['inline_keyboard'][0][0]['web_app']['url'] == 'https://example.com/orders'


@pytest.mark.asyncio
async def test_network_failure_never_leaks_bot_token():
    async def handler(request):
        raise httpx.ConnectError('failure at ' + str(request.url), request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(TelegramError) as error:
            await TelegramClient('123:SECRET', transport).call('getMe')
        assert 'SECRET' not in str(error.value)
        assert error.value.__suppress_context__


def test_bot_purchase_copy_is_shared_between_payment_modes():
    copy, markup = reply_for('/start@appki_test_bot', settings())
    live = settings()
    live.yookassa_mode = 'live'
    assert reply_for('/start@appki_test_bot', live) == (copy, markup)
    assert markup['inline_keyboard'][0][0]['web_app']['url'] == 'https://example.com/'


def test_polling_cursor_survives_restart(tmp_path):
    path = tmp_path / 'bot.offset'
    assert read_offset(path) == 0
    save_offset(path, 32)
    assert read_offset(path) == 32
    assert path.stat().st_mode & 0o777 == 0o600
