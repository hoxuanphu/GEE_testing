"""Stream a frozen public GEC object and read small overview previews."""
from functools import lru_cache
from inspect import signature
from io import BytesIO, RawIOBase
import os
import re
from time import monotonic
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
import numpy as np
from PIL import Image
import rasterio
import requests

from .umbra_catalog import object_url, small_get


CHUNK_SIZE = 1024**2
PREVIEW_LIMIT = 8 * 1024**2
PREVIEW_PIXELS = 512
_HAS_OPENER = 'opener' in signature(rasterio.open).parameters


def attachment(filename):
    filename = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', '_', filename).strip(' .')
    return "attachment; filename*=UTF-8''" + quote(filename or 'umbra', safe='')


def _chunks(response, expected):
    sent = 0
    for part in response.iter_content(CHUNK_SIZE):
        if part:
            sent += len(part)
            if sent > expected:
                raise OSError('Umbra response exceeds declared length')
            yield part
    if sent != expected:
        raise OSError('Umbra response ended before the complete file')


class _AssetResponse(StreamingResponse):
    def __init__(self, response, scene):
        self.upstream = response
        super().__init__(_chunks(response, scene['size_bytes']), media_type='image/tiff', headers={
            'Content-Length': str(scene['size_bytes']),
            'Content-Disposition': attachment(scene['filename']),
            'X-Content-Type-Options': 'nosniff',
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.upstream.close()


def download(scene):
    try:
        response = requests.get(object_url(scene['object_path']), stream=True, allow_redirects=False,
                                timeout=(8, 25), headers={
                                    'If-Match': scene['etag'], 'Accept-Encoding': 'identity',
                                })
    except requests.RequestException:
        raise HTTPException(502, 'Không kết nối được bucket Umbra.') from None
    if response.status_code in (404, 412) or (
        response.status_code == 200 and response.headers.get('ETag') != scene['etag']
    ):
        response.close()
        raise HTTPException(409, 'Tệp Umbra đã thay đổi hoặc không còn. Hãy tìm lại cảnh.')
    if (response.status_code != 200 or response.headers.get('Content-Length') != str(scene['size_bytes'])
            or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
        response.close()
        raise HTTPException(502, 'Umbra trả dữ liệu không khớp với thông tin tệp đã chọn.')
    return _AssetResponse(response, scene)


class _RangeSource:
    """Share one byte budget and block cache between GDAL file handles."""
    block_size = 128 * 1024

    def __init__(self, session, path, etag, size):
        self.session = session
        self.url = object_url(path)
        self.etag, self.size = etag, size
        self.fetched = 0
        self.deadline = monotonic() + 25
        self.blocks = {}

    def open(self, path, mode='rb'):
        if path != 'preview.tif':
            raise FileNotFoundError(path)
        return _RangeFile(self)

    def block(self, start):
        if start not in self.blocks:
            self.blocks[start] = self._fetch(start)
        return self.blocks[start]

    def _fetch(self, start):
        stop = min(start + self.block_size, self.size)
        length = stop - start
        if self.fetched + length > PREVIEW_LIMIT or monotonic() >= self.deadline:
            raise OSError('Umbra preview exceeds transfer budget')
        self.fetched += length
        with self.session.get(self.url, stream=True, allow_redirects=False, timeout=(4, 5), headers={
            'Range': f'bytes={start}-{stop - 1}', 'If-Match': self.etag, 'Accept-Encoding': 'identity',
        }) as response:
            if (response.status_code != 206 or response.headers.get('ETag') != self.etag or
                    response.headers.get('Content-Range') != f'bytes {start}-{stop - 1}/{self.size}' or
                    response.headers.get('Content-Length') != str(length) or
                    response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                raise OSError('Umbra did not provide the requested byte range')
            data = bytearray()
            for chunk in response.iter_content(32768):
                data.extend(chunk)
                if len(data) > length or monotonic() >= self.deadline:
                    raise OSError('Umbra preview exceeded response budget')
            if len(data) != length:
                raise OSError('Umbra preview returned an incomplete byte range')
            return bytes(data)


class _RangeFile(RawIOBase):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_CUR:
            offset += self.position
        elif whence == os.SEEK_END:
            offset += self.source.size
        if offset < 0:
            raise OSError('Invalid seek before start of Umbra preview')
        self.position = offset
        return offset

    def read(self, size=-1):
        end = self.source.size if size < 0 else min(self.position + size, self.source.size)
        if end - self.position > PREVIEW_LIMIT:
            raise OSError('Umbra preview exceeds read budget')
        data = bytearray()
        while self.position < end:
            start = (self.position // self.source.block_size) * self.source.block_size
            block = self.source.block(start)
            take = min(end - self.position, start + len(block) - self.position)
            offset = self.position - start
            data.extend(block[offset:offset + take])
            self.position += take
        return bytes(data)

def _jpeg(image):
    image = image.convert('L')
    image.thumbnail((PREVIEW_PIXELS, PREVIEW_PIXELS))
    output = BytesIO()
    image.save(output, format='JPEG', quality=82)
    return output.getvalue()


@lru_cache(maxsize=128)
def thumbnail(path, etag, size, preview_path=None, preview_etag=None):
    with requests.Session() as session:
        if preview_path:
            try:
                content = small_get(session, object_url(preview_path), headers={'If-Match': preview_etag},
                                    max_bytes=512 * 1024)
                with Image.open(BytesIO(content)) as image:
                    if max(image.size) <= 2048:
                        return _jpeg(image)
            except (OSError, ValueError, requests.RequestException, Image.DecompressionBombError):
                pass
        if not _HAS_OPENER:
            raise OSError('Rasterio does not support a bounded remote opener')
        source = _RangeSource(session, path, etag, size)
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
            with rasterio.open('preview.tif', opener=source.open, driver='GTiff') as dataset:
                overviews = dataset.overviews(1)
                if not overviews:
                    raise OSError('Umbra GeoTIFF has no overview')
                scale = max(max(dataset.width, dataset.height) / PREVIEW_PIXELS, 1)
                index = min(range(len(overviews)), key=lambda i: abs(overviews[i] - scale))
            with rasterio.open('preview.tif', opener=source.open, driver='GTiff',
                               OVERVIEW_LEVEL=index) as overview:
                scale = max(max(overview.width, overview.height) / PREVIEW_PIXELS, 1)
                pixels = overview.read(1, out_shape=(max(1, round(overview.height / scale)),
                                                     max(1, round(overview.width / scale))), masked=True)
    values = np.asarray(pixels.data, dtype=np.float32)
    valid = ~np.ma.getmaskarray(pixels) & np.isfinite(values)
    if not valid.any():
        raise OSError('Umbra overview has no valid pixels')
    low, high = np.percentile(values[valid], (2, 98))
    output = np.zeros(values.shape, dtype=np.uint8)
    if high > low:
        output[valid] = np.clip((values[valid] - low) * 255 / (high - low), 0, 255).astype(np.uint8)
    return _jpeg(Image.fromarray(output))
