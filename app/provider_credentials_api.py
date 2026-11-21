"""Loopback-only browser setup for providers used by the local backend."""
import ipaddress
import json
import re

import ee
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from .provider_credentials import (
    PROVIDERS, CredentialSettingsError, active_credentials, normalize_settings,
    read_saved, save_settings,
)


router = APIRouter(prefix='/api/local-provider-settings', include_in_schema=False)
_LOCAL_HOST = re.compile(r'(localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?', re.IGNORECASE)
_MAX_BODY_BYTES = 32 * 1024


def _reply(body, status=200):
    return JSONResponse(body, status_code=status, headers={'Cache-Control': 'no-store'})


def _is_local(request):
    try:
        address = ipaddress.ip_address(request.client.host) if request.client else None
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(name in request.headers for name in ('forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto')):
        return False
    hosts = request.headers.getlist('host')
    if not address or not address.is_loopback or len(hosts) != 1 or not _LOCAL_HOST.fullmatch(hosts[0]):
        return False
    origins = request.headers.getlist('origin')
    if not origins and request.method == 'GET':
        return True
    return origins == [f'{request.url.scheme}://{hosts[0].lower()}']


def _forbidden():
    return _reply({'detail': 'Mở ứng dụng bằng localhost hoặc 127.0.0.1 để cấu hình nguồn dữ liệu.'}, 403)


@router.get('')
def get_status(request: Request):
    if not _is_local(request):
        return _forbidden()
    result = {}
    for provider in PROVIDERS:
        try:
            saved = read_saved(provider)
        except CredentialSettingsError:
            state = 'error'
        else:
            state = ('not_saved' if saved is None else
                     'active' if saved == active_credentials(provider) else 'restart_required')
        result[provider] = state
    return _reply(result)


@router.put('/{provider}')
async def update_settings(provider: str, request: Request):
    if not _is_local(request):
        return _forbidden()
    if provider not in PROVIDERS:
        return _reply({'detail': 'Nguồn dữ liệu không hợp lệ.'}, 404)
    if request.headers.get('content-type', '').partition(';')[0].strip().lower() != 'application/json':
        return _reply({'detail': 'Cấu hình phải được gửi dưới dạng JSON.'}, 415)
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_BODY_BYTES:
                return _reply({'detail': 'Tệp cấu hình vượt quá 32 KB.'}, 413)
            raw.extend(chunk)
        payload = json.loads(raw)
    except (ValueError, RecursionError, ClientDisconnect):
        return _reply({'detail': 'Không đọc được cấu hình JSON.'}, 400)
    try:
        settings = normalize_settings(provider, payload)
        if provider == 'gee':
            try:
                await run_in_threadpool(ee.ServiceAccountCredentials, key_data=json.dumps(settings))
            except (TypeError, ValueError, ee.EEException) as exc:
                raise CredentialSettingsError('Khóa riêng trong tệp GEE không hợp lệ.') from exc
    except CredentialSettingsError as exc:
        return _reply({'detail': str(exc)}, 422)
    previous = active_credentials(provider)
    try:
        await run_in_threadpool(save_settings, provider, settings)
    except OSError:
        return _reply({'detail': 'Không thể ghi cấu hình vào thư mục ứng dụng.'}, 500)
    return _reply({'state': 'active' if settings == previous else 'restart_required'})
