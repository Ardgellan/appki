"""Telegram WebApp HMAC authentication. Never trust initDataUnsafe."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class InvalidTelegramData(ValueError):
    pass


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def validate_init_data(init_data: str, bot_token: str, *, max_age: int = 3600, now: int | None = None) -> dict:
    if not bot_token or not init_data or len(init_data) > 16384:
        raise InvalidTelegramData('Invalid Telegram authentication')
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError('Duplicate fields')
        supplied = data.pop('hash')
        if not re.fullmatch('[a-fA-F0-9]{64}', supplied):
            raise ValueError('Invalid hash')
        check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
        secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied.lower()):
            raise ValueError('Invalid hash')
        auth_date = int(data['auth_date'])
        clock = int(time.time()) if now is None else now
        if auth_date > clock + 30 or clock - auth_date > max_age:
            raise ValueError('Expired authentication')
        user = json.loads(data['user'])
        if (not isinstance(user, dict) or type(user.get('id')) is not int or
                not 0 < user['id'] < 2**63 or not isinstance(user.get('first_name'), str)):
            raise ValueError('Invalid user')
        result = {'id': user['id'], 'first_name': user['first_name'][:256]}
        if isinstance(user.get('username'), str):
            result['username'] = user['username'][:64]
        return result
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise InvalidTelegramData('Invalid Telegram authentication') from exc
