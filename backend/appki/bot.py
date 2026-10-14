"""Small Telegram long-polling worker; customer data and API tokens are never logged."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from appki.config import Settings

log = logging.getLogger('appki.bot')


class TelegramError(Exception):
    def __init__(self, code: int = 0, retry_after: int = 5):
        self.code = code
        self.retry_after = max(1, min(retry_after, 60))
        super().__init__(f'Telegram API error {code}')


class TelegramClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._base = f'https://api.telegram.org/bot{token}/'
        self._owned = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), follow_redirects=False)

    async def call(self, method: str, **payload):
        try:
            response = await self._client.post(self._base + method, json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise TelegramError() from None
        if not isinstance(data, dict) or data.get('ok') is not True:
            code = data.get('error_code', response.status_code) if isinstance(data, dict) else response.status_code
            params = data.get('parameters', {}) if isinstance(data, dict) else {}
            retry = params.get('retry_after', 5) if isinstance(params, dict) else 5
            raise TelegramError(code if isinstance(code, int) else 0, retry if isinstance(retry, int) else 5)
        return data.get('result')

    async def close(self):
        if self._owned:
            await self._client.aclose()


def keyboard(settings: Settings, path: str = '/') -> dict:
    rows = [[{'text': 'Открыть каталог' if path == '/' else 'Мои приложения',
              'web_app': {'url': settings.public_base_url + path}}]]
    support = settings.support_url
    parsed = urlsplit(support)
    if parsed.scheme == 'https' and parsed.hostname:
        rows.append([{'text': 'Поддержка', 'url': support}])
    return {'inline_keyboard': rows}


def reply_for(text: str, settings: Settings) -> tuple[str, dict]:
    command = text.strip().split(maxsplit=1)[0].split('@')[0].lower() if text.strip() else ''
    if command == '/purchases':
        return ('Ваши заказы, ссылки на установку и инструкция по активации находятся в разделе «Мои приложения».', keyboard(settings, '/orders'))
    if command in ('/support', '/paysupport'):
        copy = ('По вопросам установки и оплаты напишите в поддержку. Укажите номер заказа из раздела «Мои приложения». '
                'Пароли и коды своего Apple Account присылать не нужно.')
        if not settings.support_url:
            copy = 'Контакт поддержки пока не настроен. Попробуйте обратиться позже.'
        return copy, keyboard(settings, '/orders')
    if command == '/terms':
        return ('Аппки помогает установить приложение на iPhone. До оплаты проверьте совместимость и способ активации в карточке. '
                'Для первого запуска может понадобиться авторизация в App Store. '
                'Установка не гарантирует бессрочную работу: доступ зависит также от Apple и разработчика приложения. '
                'Вопросы оплаты и возврата решаются через поддержку.', keyboard(settings))
    if command == '/help':
        return ('1. Выберите приложение в каталоге.\n2. Оплатите заказ через ЮKassa.\n'
                '3. Откройте ссылку установки в Safari на iPhone.\n'
                '4. Установите приложение и выполните инструкцию активации.\n\n'
                'Основной аккаунт iCloud менять не нужно.', keyboard(settings))
    return (f'{settings.appki_brand} — приложения для вашего iPhone.\n\n'
            'Откройте каталог: внутри описание, совместимость, оплата и пошаговая установка.',
            keyboard(settings))


def read_offset(path: Path) -> int:
    try:
        return max(0, int(path.read_text().strip()))
    except (OSError, ValueError):
        return 0


def save_offset(path: Path, offset: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(str(offset))
    temp.chmod(0o600)
    os.replace(temp, path)


async def handle_update(client: TelegramClient, update: dict, settings: Settings):
    message = update.get('message')
    if not isinstance(message, dict) or message.get('chat', {}).get('type') != 'private':
        return
    chat_id = message.get('chat', {}).get('id')
    if not isinstance(chat_id, int):
        return
    text, markup = reply_for(message.get('text', ''), settings)
    await client.call('sendMessage', chat_id=chat_id, text=text, reply_markup=markup,
                      link_preview_options={'is_disabled': True})


async def run(settings: Settings):
    if not settings.bot_token:
        log.info('BOT_TOKEN не заполнен. Бот ожидает настройки.')
        return
    client = TelegramClient(settings.bot_token)
    offset_path = settings.database_path.parent / 'bot.offset'
    offset = read_offset(offset_path)
    try:
        me = await client.call('getMe')
        if not isinstance(me, dict) or not me.get('is_bot'):
            raise RuntimeError('Telegram did not confirm bot identity')
        if settings.bot_username and me.get('username', '').lower() != settings.bot_username.lstrip('@').lower():
            raise RuntimeError('BOT_USERNAME does not match BOT_TOKEN')
        webhook = await client.call('getWebhookInfo')
        if isinstance(webhook, dict) and webhook.get('url'):
            raise RuntimeError('Bot already has a webhook; remove it deliberately before using this worker')
        await client.call('setMyCommands', commands=[
            {'command': 'start', 'description': 'Открыть Аппки'},
            {'command': 'apps', 'description': 'Каталог приложений'},
            {'command': 'purchases', 'description': 'Мои приложения и заказы'},
            {'command': 'help', 'description': 'Как установить приложение'},
            {'command': 'support', 'description': 'Помощь с установкой'},
            {'command': 'paysupport', 'description': 'Вопросы оплаты'},
            {'command': 'terms', 'description': 'Условия установки'},
        ])
        await client.call('setChatMenuButton', menu_button={
            'type': 'web_app', 'text': 'Каталог', 'web_app': {'url': settings.public_base_url + '/'}})
        log.info('Бот подключён. Ожидание сообщений.')
        while True:
            try:
                updates = await client.call('getUpdates', offset=offset, timeout=30, allowed_updates=['message'])
                if not isinstance(updates, list):
                    raise TelegramError()
                for update in updates:
                    if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
                        continue
                    if update['update_id'] < offset:
                        continue
                    try:
                        await handle_update(client, update, settings)
                    except TelegramError as exc:
                        # A blocked bot / deleted chat must not poison the whole queue.
                        if exc.code not in (400, 403):
                            raise
                    offset = update['update_id'] + 1
                    save_offset(offset_path, offset)
            except TelegramError as exc:
                if exc.code in (401, 409):
                    log.error('Бот остановлен: неверный токен или уже запущен другой обработчик (код %s).', exc.code)
                    return
                log.warning('Telegram временно недоступен (код %s). Повтор подключения.', exc.code)
                await asyncio.sleep(exc.retry_after)
    finally:
        await client.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    logging.getLogger('httpcore').setLevel(logging.CRITICAL)
    try:
        asyncio.run(run(Settings()))
    except KeyboardInterrupt:
        pass
    except TelegramError as exc:
        log.error('Подключение к Telegram не выполнено (код %s).', exc.code)
        raise SystemExit(1) from None
    except Exception:
        # Settings validation can include raw secrets. Do not print exception objects.
        log.error('Не удалось запустить бота. Проверьте настройки, права файлов и отсутствие другого обработчика.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
