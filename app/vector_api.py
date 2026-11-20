"""Bounded vector upload and immutable polygon ROI references for local map selection."""
from dataclasses import dataclass
from typing import Annotated

import anyio
import ee
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .geo import Bbox
from .vector_import import MAX_FILE_BYTES, VectorImportError, read_vector


@dataclass
class VectorArea:
    id: str
    name: str
    geometry: dict
    bbox: Bbox

    def ee_geometry(self):
        return ee.Geometry(self.geometry, geodesic=False)


def vector_area(store, roi_id):
    try:
        record = store.get_roi(roi_id)
    except VectorImportError as exc:
        raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from None
    return VectorArea(record['roi_id'], record['name'], record['geometry'], Bbox(**record['bbox']))


class FeatureSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    import_id: str = Field(min_length=1, max_length=64)
    feature_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=10_000)


class ROIRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    selections: list[FeatureSelection] = Field(min_length=1, max_length=10_000)


def register_vector_routes(api, store):
    router = APIRouter(prefix='/api/vectors')

    def invoke(operation, *args):
        try:
            return operation(*args)
        except VectorImportError as exc:
            raise HTTPException(exc.status, {'code': exc.code, 'message': exc.message}) from None

    @router.post('/import')
    async def import_file(request: Request):
        maximum = MAX_FILE_BYTES + 64 * 1024
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            raise HTTPException(400, 'Kích thước upload không hợp lệ.') from None
        if length > maximum:
            raise HTTPException(413, 'File vector vượt giới hạn 20 MiB.')
        if not request.headers.get('content-type', '').lower().startswith('multipart/form-data'):
            raise HTTPException(400, 'Gửi file vector bằng multipart/form-data.')
        received, too_large = 0, False

        async def bounded_stream():
            nonlocal received, too_large
            async for chunk in request.stream():
                received += len(chunk)
                if received > maximum:
                    too_large = True
                    raise MultiPartException('Vector upload too large')
                yield chunk

        try:
            form = await MultiPartParser(request.headers, bounded_stream(), max_files=1,
                                         max_fields=0, max_part_size=MAX_FILE_BYTES).parse()
        except MultiPartException:
            raise HTTPException(413 if too_large else 400,
                                'File vector vượt giới hạn 20 MiB.' if too_large else 'Chọn đúng một file vector hợp lệ.') from None
        try:
            uploads = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
            if len(uploads) != 1 or 'file' not in form:
                raise HTTPException(400, 'Chọn đúng một file vector cho mỗi yêu cầu nhập.')
            upload = uploads[0]
            data = await upload.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(413, 'File vector vượt giới hạn 20 MiB.')
            payload = await anyio.to_thread.run_sync(lambda: invoke(read_vector, data, upload.filename or 'vector'))
            return await anyio.to_thread.run_sync(lambda: invoke(store.save_import, payload))
        finally:
            await form.close()

    @router.get('/imports/{import_id}')
    def get_import(import_id: str):
        return invoke(store.get_import, import_id)

    @router.delete('/imports/{import_id}', status_code=204)
    def delete_import(import_id: str):
        invoke(store.delete_import, import_id)
        return Response(status_code=204)

    @router.post('/rois')
    def create_roi(body: ROIRequest):
        return invoke(store.create_roi, [selection.model_dump() for selection in body.selections])

    @router.get('/rois/{roi_id}')
    def get_roi(roi_id: str):
        return invoke(store.get_roi, roi_id)

    api.include_router(router)
