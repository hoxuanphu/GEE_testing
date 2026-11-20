"""Bounded metadata search and safe diagnostics shared by GEE search routes."""
from __future__ import annotations

import logging
import time
import traceback

import requests
from google.auth.exceptions import DefaultCredentialsError, RefreshError

SEARCH_SECONDS = 120
MAX_SCENES = 20_000
_logger = logging.getLogger('uvicorn.error')


class MetadataSearchError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _causes(exc):
    chain = []
    while exc is not None and len(chain) < 10 and all(exc is not item for item in chain):
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    return chain


def metadata_error(exc: Exception) -> MetadataSearchError:
    if isinstance(exc, MetadataSearchError):
        return exc
    chain = _causes(exc)
    if any(isinstance(item, (DefaultCredentialsError, RefreshError)) or
           getattr(item, 'http_status', None) in (401, 403) or
           getattr(item, 'code', None) in ('upstream_unauthenticated', 'upstream_forbidden')
           for item in chain):
        return MetadataSearchError('upstream_auth', 'GEE không xác thực được hoặc từ chối quyền truy cập collection.')
    if any(isinstance(item, (TimeoutError, requests.Timeout)) or
           getattr(item, 'http_status', None) in (408, 504) or
           getattr(item, 'code', None) == 'timeout' for item in chain):
        return MetadataSearchError('timeout', 'GEE hết thời gian tìm ảnh; hãy thu hẹp khoảng ngày hoặc dùng tải theo năm.', 504)
    if any(getattr(item, 'http_status', None) == 429 or
           getattr(item, 'code', None) == 'upstream_quota' for item in chain):
        return MetadataSearchError('upstream_quota', 'GEE vượt hạn mức truy vấn; hãy thử lại sau.', 503)
    if any(getattr(item, 'code', None) == 'response_too_large' for item in chain):
        return MetadataSearchError('result_limit', 'Danh sách ảnh vượt giới hạn; hãy thu hẹp khoảng ngày hoặc dùng tải theo năm.', 422)
    return MetadataSearchError('upstream_failed', 'Không thể lấy đầy đủ danh sách ảnh từ GEE; hãy thử lại sau.')


def compute_metadata(graph):
    # Lazy import: frame_search also imports imagery. Reuse its isolated REST
    # session and bounded retries without mutating the process-wide EE transport.
    from .frame_search import _RPC

    # Cooperative evaluation budget: buffered socket reads may return late.
    # The shared transport checks between reads and rejects every late result.
    deadline = time.monotonic() + SEARCH_SECONDS
    try:
        with _RPC(SEARCH_SECONDS, deadline=deadline) as rpc:
            rows = rpc.compute(graph)
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MetadataSearchError('invalid_metadata', 'GEE trả về danh sách metadata không hợp lệ.')
        if len(rows) > MAX_SCENES:
            raise MetadataSearchError('result_limit', 'Danh sách ảnh vượt giới hạn; hãy thu hẹp khoảng ngày hoặc dùng tải theo năm.', 422)
        return rows
    except MetadataSearchError:
        raise
    except Exception as exc:
        raise metadata_error(exc) from exc


def search_failure(exc: Exception, *, stage: str, request_id: str, started: float) -> MetadataSearchError:
    if stage == 'snapshot':
        error = MetadataSearchError('snapshot_failed', 'Không thể lưu kết quả tìm ảnh trên máy chủ; hãy kiểm tra ổ đĩa và thử lại.', 500)
    elif stage == 'validation':
        error = MetadataSearchError('invalid_metadata', 'GEE trả về metadata cảnh không hợp lệ; kết quả chưa được lưu.')
    elif stage == 'parameters':
        error = MetadataSearchError('search_failed', 'Không thể chuẩn bị kết quả tìm ảnh trên máy chủ.', 500)
    else:
        error = metadata_error(exc)
        if stage == 'initialization' and error.code == 'upstream_failed':
            error = MetadataSearchError('initialization_failed', 'Không thể khởi tạo phiên GEE; hãy kiểm tra kết nối và cấu hình xác thực.', 503)
    # Keep exception types and stack locations; provider messages can contain
    # signed URLs, credentials or local paths and must not reach logs/responses.
    chain = _causes(exc)
    locations = ' '.join(f'{frame.name}:{frame.lineno}' for item in chain
                         for frame in traceback.extract_tb(item.__traceback__))
    _logger.error('gee_search_failed request_id=%s stage=%s code=%s error_type=%s elapsed_ms=%.2f stack=%s',
                  request_id, stage, error.code, type(chain[-1]).__name__,
                  (time.monotonic() - started) * 1000, locations)
    return error
