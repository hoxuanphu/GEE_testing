"""Local provider settings staged on disk and read once per backend process."""
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile


_SETTINGS_DIR = Path(__file__).resolve().parent.parent
PROVIDERS = ('gee', 'cdse', 's3')
_GOOGLE_TOKEN_URI = 'https://oauth2.googleapis.com/token'


class CredentialSettingsError(ValueError):
    pass


def _settings_path(provider):
    if provider not in PROVIDERS:
        raise CredentialSettingsError('Nguồn dữ liệu không hợp lệ.')
    return _SETTINGS_DIR / f'.env.providers.{provider}.json'


def _required_text(payload, key, limit):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CredentialSettingsError('Cấu hình thiếu trường bắt buộc hoặc giá trị quá dài.')
    if key != 'private_key' and not value.isprintable():
        raise CredentialSettingsError('Cấu hình chứa ký tự không hợp lệ.')
    return value


def normalize_settings(provider, payload):
    if not isinstance(payload, dict):
        raise CredentialSettingsError('Cấu hình phải là đối tượng JSON.')
    if provider == 'gee':
        if payload.get('type') != 'service_account':
            raise CredentialSettingsError('Chọn tệp JSON của Google service account.')
        return {
            'type': 'service_account',
            'project_id': _required_text(payload, 'project_id', 128).strip(),
            'client_email': _required_text(payload, 'client_email', 320).strip(),
            'private_key_id': _required_text(payload, 'private_key_id', 128).strip(),
            'private_key': _required_text(payload, 'private_key', 16_384),
            'token_uri': _GOOGLE_TOKEN_URI,
        }
    if provider == 'cdse':
        return {
            'username': _required_text(payload, 'username', 320).strip(),
            'password': _required_text(payload, 'password', 1024),
        }
    if provider == 's3':
        return {
            'access_key': _required_text(payload, 'access_key', 256).strip(),
            'secret_key': _required_text(payload, 'secret_key', 1024).strip(),
        }
    raise CredentialSettingsError('Nguồn dữ liệu không hợp lệ.')


def read_saved(provider):
    try:
        data = _settings_path(provider).read_text(encoding='utf-8')
        return normalize_settings(provider, json.loads(data))
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise CredentialSettingsError('Không đọc được cấu hình cục bộ đã lưu.') from exc


@lru_cache(maxsize=len(PROVIDERS))
def active_credentials(provider):
    try:
        return read_saved(provider)
    except CredentialSettingsError:
        return None


def save_settings(provider, settings):
    path = _settings_path(provider)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent, prefix='.env.providers.', delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(settings, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
