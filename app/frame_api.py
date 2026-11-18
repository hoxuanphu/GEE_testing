"""Reference upload, immutable searches and verified result downloads."""
from __future__ import annotations

import anyio
from pathlib import Path
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.datastructures import UploadFile

from .frame_models import (FrameError, FrameSearchRequest, FramePreflightRequest, FrameJobRequest,
                           MAX_BODY_BYTES, MAX_UPLOAD_BYTES)
from .frame_store import identifier
from .reference_frame import read_reference_metadata

PREFIX = '/api/matched-frame'


def error_response(status, code, message):
    return JSONResponse({'detail': {'code': code, 'message': message}}, status_code=status)


class FrameUploadLimitMiddleware:
    def __init__(self, app, body_limit=MAX_BODY_BYTES):
        self.app, self.body_limit = app, body_limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith(PREFIX + '/'):
            return await self.app(scope, receive, send)
        headers = {k.decode('latin-1').lower(): v.decode('latin-1') for k, v in scope.get('headers', [])}
        if scope.get('method') == 'POST':
            origin = headers.get('origin')
            allowed_origin = f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"
            if ((origin is not None and origin.rstrip('/') != allowed_origin) or
                    headers.get('sec-fetch-site') == 'cross-site'):
                return await error_response(403, 'invalid_origin', 'Chỉ nhận yêu cầu từ cùng ứng dụng.')(scope, receive, send)
        if scope.get('method') != 'POST' or scope['path'] != PREFIX + '/references':
            return await self.app(scope, receive, send)
        try:
            if int(headers.get('content-length', '0')) > self.body_limit:
                return await error_response(413, 'upload_too_large', 'Ảnh mẫu vượt giới hạn 256 MiB.')(scope, receive, send)
        except ValueError:
            return await error_response(400, 'invalid_length', 'Kích thước request không hợp lệ.')(scope, receive, send)
        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            if message['type'] == 'http.request':
                size += len(message.get('body', b''))
                if size > self.body_limit:
                    scope['frame_upload_too_large'] = True
                    # Starlette closes already-open multipart spool files on this exception.
                    raise MultiPartException('upload_too_large')
            return message

        await self.app(scope, bounded_receive, send)


class FrameRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validate(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return error_response(422, 'invalid_request', 'Thông số không hợp lệ; kiểm tra ngày, nguồn, lưới và cảnh đã chọn.')
        return validate


class LeasedFileResponse(FileResponse):
    def __init__(self, path, store, key, **kwargs):
        super().__init__(path, **kwargs)
        self.store, self.key = store, key

    async def __call__(self, scope, receive, send):
        with self.store.lease(self.key):
            await super().__call__(scope, receive, send)


def register_frame_routes(app, get_service):
    router = APIRouter(prefix=PREFIX, tags=['matched-frame'], route_class=FrameRoute)

    def service():
        return get_service() if callable(get_service) else get_service

    @router.post('/references')
    async def upload_reference(request: Request):
        form = None
        temporary = None
        try:
            form = await request.form(max_files=1, max_fields=0)
            files = list(form.multi_items())
            if len(files) != 1 or files[0][0] != 'file' or not isinstance(files[0][1], UploadFile):
                raise FrameError('invalid_upload', 'Chọn đúng một tệp GeoTIFF có trường file.')
            upload = files[0][1]
            if Path(upload.filename or '').suffix.lower() not in ('.tif', '.tiff'):
                raise FrameError('unsupported_file', 'Chọn tệp .tif hoặc .tiff.', 415)
            upload_dir = service().store.root / 'upload'
            upload_dir.mkdir(exist_ok=True)
            temporary = upload_dir / f'{uuid.uuid4()}.tif'
            total = 0
            with temporary.open('xb') as output:
                while True:
                    block = await upload.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_UPLOAD_BYTES:
                        raise FrameError('upload_too_large', 'Ảnh mẫu vượt giới hạn 256 MiB.', 413)
                    await anyio.to_thread.run_sync(output.write, block)
            metadata = await anyio.to_thread.run_sync(read_reference_metadata, temporary, upload.filename or 'reference.tif')
            saved = service().store.save_reference(metadata)
            return {**saved, 'footprint_geojson': metadata['footprint_geojson'],
                    'compatible_grid_modes': metadata['compatible_grid_modes'],
                    'warnings': ['Chỉ dùng toàn khung affine; không dùng alpha/NoData ảnh mẫu làm mask xuất.']}
        except FrameError as exc:
            return error_response(exc.status, exc.code, exc.message)
        except (MultiPartException, StarletteHTTPException):
            if request.scope.get('frame_upload_too_large'):
                return error_response(413, 'upload_too_large', 'Ảnh mẫu vượt giới hạn 256 MiB.')
            return error_response(400, 'invalid_multipart', 'Tệp upload không hợp lệ; chọn một GeoTIFF và thử lại.')
        except Exception:
            return error_response(422, 'upload_failed', 'Không đọc được ảnh mẫu; kiểm tra tệp và thử lại.')
        finally:
            if form is not None:
                await form.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def invoke(function, *args):
        try:
            return function(*args)
        except FrameError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from None
        except Exception:
            raise HTTPException(502, {'code': 'upstream_failed', 'message': 'Không truy xuất được dữ liệu; hãy thử lại.'}) from None

    def page(search_id, offset=0, limit=50):
        data = invoke(service().search_page, search_id, offset, limit)
        for scene in data['scenes']:
            scene['preview_url'] = f"{PREFIX}/searches/{data['search_id']}/scenes/{scene['scene_token']}/preview"
        return data

    @router.get('/references/{reference_id}')
    def get_reference(reference_id: str):
        return invoke(service().store.get_reference, reference_id)

    @router.post('/searches')
    def search(body: FrameSearchRequest):
        result = invoke(service().search, body)
        return page(result['search_id'])

    @router.get('/searches/{search_id}')
    def get_search(search_id: str, offset: int = 0, limit: int = 50):
        return page(search_id, offset, limit)

    @router.get('/searches/{search_id}/scenes/{scene_token}/preview')
    def preview(search_id: str, scene_token: str):
        invoke(identifier, scene_token)
        image = invoke(service().preview, search_id, scene_token)
        return Response(image, media_type='image/png', headers={'Cache-Control': 'private, max-age=300'})

    @router.post('/preflight')
    def preflight(body: FramePreflightRequest):
        result = invoke(service().preflight, body.search_id, body.scene_tokens)
        if result['parameters'].get('merge_scenes'):
            result['preview_url'] = f"{PREFIX}/preflights/{result['preflight_id']}/preview"
        return result

    @router.get('/preflights/{preflight_id}/preview')
    def selection_preview(preflight_id: str):
        content = invoke(service().selection_preview, preflight_id)
        return Response(content, media_type='image/png', headers={'Cache-Control': 'private, max-age=300'})

    def job_response(job):
        key = job['job_id']
        for scene in job['scenes']:
            if scene.get('file_token') and scene['status'] == 'succeeded':
                scene['download_url'] = f"{PREFIX}/jobs/{key}/files/{scene['file_token']}"
        job['download_url'] = f'{PREFIX}/jobs/{key}/download' if job['status'] in ('completed', 'partial') else None
        return job

    @router.post('/jobs')
    def create_job(body: FrameJobRequest):
        return job_response(invoke(service().start_job, body.preflight_id, body.digest, body.acknowledged))

    @router.get('/jobs/{job_id}')
    def get_job(job_id: str):
        return job_response(invoke(service().store.get_job, job_id))

    def download(job_id, token=None):
        store = service().store
        key = invoke(identifier, job_id)
        with store.lease(key):
            path, name = invoke(store.result_path, key, token)
        return LeasedFileResponse(path, store, key, filename=name,
                                  media_type='image/tiff' if token else 'application/zip')

    @router.get('/jobs/{job_id}/files/{file_token}')
    def download_file(job_id: str, file_token: str):
        return download(job_id, file_token)

    @router.get('/jobs/{job_id}/download')
    def download_zip(job_id: str):
        return download(job_id)

    app.add_middleware(FrameUploadLimitMiddleware)
    app.include_router(router)
