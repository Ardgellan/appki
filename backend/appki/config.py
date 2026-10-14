from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore', case_sensitive=False, hide_input_in_errors=True)
    appki_brand: str = 'Аппки'
    public_base_url: str = 'https://85.239.144.214'
    bot_token: str = ''
    bot_username: str = ''
    support_url: str = ''
    database_path: Path = Path('work/appki.sqlite3')
    static_dir: Path = Path('web/dist')
    happ_ipa_path: Path = Path('/var/lib/appki/apps/Happ.ipa')
    happ_ipa_sha256: str = '94e9591fe92be4589c19344ab7089d0b6abe101cf9cfa7a80707d6c390a4a2f2'
    happ_price_rub: str = '490.00'
    catalog_definition_path: Path | None = None
    artifact_registry_path: Path = Path('/var/lib/appki/artifacts/registry.json')
    artifact_root: Path = Path('/var/lib/appki/artifacts')
    activation_profiles_path: Path = Path('/etc/appki/activation-profiles.json')
    yookassa_shop_id: str = ''
    yookassa_secret_key: str = ''
    yookassa_mode: str = 'test'
    auth_max_age_seconds: int = 3600
    session_ttl_seconds: int = 86400
    install_ttl_seconds: int = 3600
    activation_mode: str = 'manual'
    activation_apple_id: str = ''
    activation_password: str = ''
    activation_instructions: str = (
        'После установки может понадобиться авторизация установочного Apple Account. '
        'Если приложение не запускается, обратитесь в поддержку. '
        'Вход выполняется только в «Контент и покупки», без выхода из iCloud. '
        'После первого успешного запуска вернитесь в свой аккаунт.'
    )

    @field_validator('public_base_url')
    @classmethod
    def valid_origin(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin')
        return value.rstrip('/')

    @field_validator('happ_price_rub')
    @classmethod
    def valid_price(cls, value):
        try:
            amount = Decimal(value)
            if not amount.is_finite() or amount <= 0 or amount > 1000000 or amount.as_tuple().exponent < -2:
                raise ValueError('Invalid price')
        except InvalidOperation as exc:
            raise ValueError('Invalid price') from exc
        return format(amount, '.2f')

    @field_validator('yookassa_mode')
    @classmethod
    def valid_mode(cls, value):
        if value not in ('test', 'live'):
            raise ValueError('YOOKASSA_MODE must be test or live')
        return value

    @field_validator('activation_mode')
    @classmethod
    def valid_activation(cls, value):
        if value not in ('manual', 'credentials'):
            raise ValueError('Invalid activation mode')
        return value

    @field_validator('auth_max_age_seconds', 'session_ttl_seconds', 'install_ttl_seconds')
    @classmethod
    def valid_ttl(cls, value):
        if not 60 <= value <= 604800:
            raise ValueError('TTL must be between 60 seconds and 7 days')
        return value

    @field_validator('install_ttl_seconds')
    @classmethod
    def installation_window(cls, value):
        if value > 3600:
            raise ValueError('Installation links last at most one hour')
        return value

    @field_validator('happ_ipa_sha256')
    @classmethod
    def valid_digest(cls, value):
        import re
        if not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('Invalid IPA SHA256')
        return value

    @model_validator(mode='after')
    def credentials_complete(self):
        if self.activation_mode == 'credentials' and not (self.activation_apple_id and self.activation_password):
            raise ValueError('Activation credentials must be configured together')
        return self

    @property
    def payments_configured(self):
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)


def get_settings() -> Settings:
    return Settings()
