"""Preflight-confirmed single-worker matched-frame jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import zipfile

from .frame_models import (FrameError, FrameScene, FrameSearchRequest, MAX_JOB_BYTES, MAX_SCENE_COUNT,
                           MIN_FREE_BYTES, bands_for, effective_min_coverage, known_positive_coverage,
                           FrameSelectionQuality)
from .frame_store import FrameStore, encode
from .raster_grid import build_reference_grid
from .reference_frame import reference_roi_identity, validate_roi_identity


def _digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def preflight_digest_payload(preflight):
    return {name: preflight.get(name) for name in (
        'search_id', 'parameters', 'scenes', 'preflight_version', 'can_start',
        'blocking_reason', 'selection_quality')}


def validate_source_platforms(scenes, params):
    from .platforms import validate_platform
    try:
        selected = validate_platform(params['dataset'], params.get('platform'))
    except ValueError:
        raise FrameError('incompatible_platforms', 'Bộ lọc vệ tinh không hợp lệ.') from None
    if selected is None:
        return
    expected = selected if params['dataset'] == 's1gee' else 'Sentinel-2' + selected
    if any(scene.get('platform') != expected for scene in scenes):
        raise FrameError('incompatible_platforms', 'Nguồn ảnh không khớp vệ tinh đã chọn.')


def validate_preflight(preflight, *, allow_blocked=False):
    params = preflight['parameters']
    validate_roi_identity(params)
    validate_source_platforms(preflight['scenes'], params)
    minimum = effective_min_coverage(params)
    try:
        if (type(preflight.get('preflight_version')) is not int or preflight['preflight_version'] != 2
                or type(preflight.get('can_start')) is not bool
                or preflight['digest'] != _digest(preflight_digest_payload(preflight))
                or preflight.get('output_grid') != params['output_grid']):
            raise ValueError()
        ready = True
        if params.get('merge_scenes'):
            quality = FrameSelectionQuality.model_validate(preflight.get('selection_quality'))
            if (quality.ordered_asset_ids != [s['asset_id'] for s in preflight['scenes']]
                    or quality.roi_fingerprint != params['roi_fingerprint']
                    or quality.total_pixels != params['output_grid']['width'] * params['output_grid']['height']):
                raise ValueError()
            ready = quality.valid_pixel_count > 0 and quality.band_coverage_pct >= minimum
        elif preflight.get('selection_quality') is not None:
            raise ValueError()
        reason = None if ready else 'insufficient_selection_coverage'
        if preflight['can_start'] != ready or preflight.get('blocking_reason') != reason:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise FrameError('stale_preflight', 'Kiểm tra trước tải không còn hợp lệ; hãy kiểm tra lại.', 409) from None
    if preflight['expires_at'] < time.time():
        raise FrameError('stale_preflight', 'Xác nhận đã hết hạn; hãy kiểm tra lại.', 409)
    if not allow_blocked and not ready:
        raise FrameError('insufficient_selection_coverage',
                         f'Nhóm chưa đạt độ phủ {minimum:g}%; hãy chọn cảnh bù vùng hoặc đổi ngưỡng.')


def validate_job_selection(job, preflight):
    """Bind queued work to the confirmed digest and exact ordered source records."""
    selected = preflight['scenes']
    if preflight['parameters'].get('merge_scenes'):
        selected = [{**selected[0], 'sources': selected}]
    runtime_fields = {'status', 'report', 'error', 'file_token', 'filename'}
    records = [{key: value for key, value in record.items() if key not in runtime_fields}
               for record in job['scenes']]
    if (job.get('preflight_digest') != preflight['digest'] or
            _digest(records) != _digest(selected)):
        raise FrameError('stale_preflight', 'Nguồn tác vụ không khớp xác nhận ban đầu; hãy kiểm tra lại.', 409)


def _safe_report(value):
    if isinstance(value, dict):
        return {str(k): _safe_report(v) for k, v in value.items()
                if not any(part in str(k).lower() for part in
                           ('url', 'password', 'credential', 'secret', 'authorization', 'cookie', 'path'))}
    if isinstance(value, (list, tuple)):
        return [_safe_report(v) for v in value]
    if isinstance(value, str):
        if '://' in value or 'Bearer ' in value or ':\\' in value or value.startswith(('\\\\', '/')):
            return '[redacted]'
        return value[:2048]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


class FrameService:
    def __init__(self, store: FrameStore):
        self.store = store
        self._lock = threading.RLock()
        self._search_lock = threading.Lock()
        self._executor = None
        self._futures = {}
        self._closed = False

    @contextmanager
    def search_slot(self):
        if not self._search_lock.acquire(blocking=False):
            raise FrameError('busy', 'Một lượt tìm ảnh đồng khung đang chạy.', 409)
        try:
            yield
        finally:
            self._search_lock.release()

    def search(self, request: FrameSearchRequest):
        from .frame_search import search_frame_scenes
        with self.search_slot():
            frame = self.store.get_reference(request.reference_id)['metadata']
            grid = build_reference_grid(frame, request.grid_mode, request.target_resolution_m)
            parameters = request.model_dump(mode='json')
            parameters.update(reference=frame, output_grid=grid, bands=bands_for(request))
            parameters.update(reference_roi_identity(frame))
            parameters.update(units='surface reflectance' if request.dataset == 'sentinel2' else 'dB',
                              resampling='bilinear' if request.dataset == 'sentinel2' else 'nearest',
                              quality_scale_m=20 if request.dataset == 'sentinel2' else 100)
            started = time.monotonic()
            scenes = search_frame_scenes(request, frame)
            if time.monotonic() - started > 300:
                raise FrameError('search_timeout', 'Tìm ảnh quá thời gian; hãy thu hẹp khoảng ngày.', 502)
            validated = []
            for scene in scenes:
                scene.pop('preview_url', None)
                scene['scene_token'] = str(scene.get('scene_token') or uuid.uuid4())
                validated.append(FrameScene.model_validate(scene).model_dump(mode='json'))
            return self.store.save_search(parameters, _safe_report(validated))

    def search_page(self, key, offset=0, limit=50):
        if offset < 0 or not 1 <= limit <= 50:
            raise FrameError('invalid_page', 'Trang kết quả không hợp lệ.')
        result = self.store.get_search(key)
        scenes = result['scenes']
        return {**result, 'total': len(scenes), 'offset': offset, 'limit': limit,
                'scenes': scenes[offset:offset + limit]}

    def preview(self, key, token, dimensions=512):
        from .frame_search import make_frame_preview
        result = self.store.get_search(key)
        validate_roi_identity(result['parameters'])
        scene = next((s for s in result['scenes'] if s['scene_token'] == str(token)), None)
        if scene is None:
            raise FrameError('not_found', 'Cảnh không thuộc kết quả tìm kiếm.', 404)
        from .frame_store import identifier
        directory = self.store.root / 'previews' / identifier(key)
        suffix = '-thumbnail' if dimensions == 192 else ''
        path = directory / f'{identifier(token)}{suffix}.png'
        with self.store.lock:
            if path.is_file():
                return path.read_bytes()
        content = (make_frame_preview(scene, result['parameters']) if dimensions == 512 else
                   make_frame_preview(scene, result['parameters'], dimensions=dimensions))
        if not content.startswith(b'\x89PNG\r\n\x1a\n') or len(content) > 8 * 1024 * 1024:
            raise FrameError('invalid_preview', 'Không đọc được ảnh xem trước.', 502)
        with self.store.lock:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f'{uuid.uuid4()}.partial'
            try:
                temporary.write_bytes(content)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return content

    def _space(self, required):
        if shutil.disk_usage(self.store.root).free < required:
            raise FrameError('disk_space', 'Không đủ dung lượng trống cho tác vụ.', 422)

    def selection_preview(self, key):
        from .frame_search import make_frame_selection_preview
        from .frame_store import identifier
        key = identifier(key)
        with self.store.lease(key):
            preflight = self.store.get_preflight(key)
            validate_preflight(preflight, allow_blocked=True)
            path = self.store.root / 'preflight_previews' / key / 'group.png'
            if path.is_file():
                return path.read_bytes()
            content = make_frame_selection_preview(preflight['scenes'], preflight['parameters'])
            if not content.startswith(b'\x89PNG\r\n\x1a\n') or len(content) > 8 * 1024**2:
                raise FrameError('invalid_preview', 'Không đọc được ảnh xem trước nhóm.', 502)
            with self.store.lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.parent / (uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_bytes(content)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            return content

    def preflight(self, search_id, scene_tokens):
        result = self.store.get_search(search_id)
        validate_roi_identity(result['parameters'])
        params = result['parameters']
        minimum = effective_min_coverage(params)
        tokens = list(map(str, scene_tokens))
        if not 1 <= len(tokens) <= MAX_SCENE_COUNT or len(tokens) != len(set(tokens)):
            raise FrameError('invalid_selection', 'Chọn từ 1 đến 10 cảnh khác nhau.')
        selected = []
        for token in tokens:
            scene = next((s for s in result['scenes'] if s['scene_token'] == token), None)
            if (scene is None or not scene.get('eligible', True)
                    or not known_positive_coverage(scene.get('band_coverage_pct'))
                    or not params.get('merge_scenes', False) and scene['band_coverage_pct'] < minimum):
                raise FrameError('invalid_selection', 'Cảnh không thuộc snapshot hoặc không đạt độ phủ yêu cầu.')
            selected.append(scene)
        validate_source_platforms(selected, params)
        params = result['parameters']
        grid = params['output_grid']
        merging = params.get('merge_scenes', False)
        if merging and params['dataset'] == 's1gee':
            track = {(s.get('orbit_pass'), s.get('relative_orbit')) for s in selected}
            if len(track) != 1 or any(p is None or o is None for p, o in track):
                raise FrameError('incompatible_orbits', 'Ghép SAR cần các cảnh cùng hướng bay và quỹ đạo tương đối.')
        output_count = 1 if merging else len(selected)
        total_bytes = grid['width'] * grid['height'] * len(params['bands']) * 4 * output_count
        if total_bytes > MAX_JOB_BYTES:
            raise FrameError('job_too_large', 'Tác vụ vượt 4 GiB chưa nén; giảm số cảnh hoặc tăng pixel.')
        from .frame_projection import estimate_export_resources
        resources = estimate_export_resources(grid, params['dataset'], params['bands'], len(selected))
        reserve = resources['disk_reserve_bytes']
        self._space(reserve)
        warnings = ['Đồng khung không phải đồng đăng ký đối tượng; nội suy không tăng chi tiết thực.']
        if merging:
            warnings.append('Ghép theo thứ tự đã chọn: giữ pixel cảnh chính, chỉ bù NoData; không xóa mây. Có thể khác ngày.')
        if params['dataset'] == 'sentinel2':
            warnings.append('SCL không bảo đảm không sương/mây mỏng; các band 20–60 m được nội suy, mây không bị xóa.')
        from rasterio.crs import CRS
        if CRS.from_user_input(grid['crs']).to_epsg() is None:
            warnings.append('CRS tùy chỉnh có thể cần warp cục bộ; nội suy không đồng nghĩa đồng đăng ký.')
        reference = params['reference']
        if params['grid_mode'] == 'match_grid':
            pixel = reference.get('pixel_size', [0])
            if reference.get('pixel_unit') != 'm' or min(pixel) < 10:
                warnings.append('Đồng lưới mẫu có thể nội suy tăng kích thước, không tăng độ phân giải thực.')
        for scene in selected:
            if (scene.get('roi_cloud_shadow_pct') or 0) > 0:
                warnings.append(f"{scene['date']}: mây/bóng mây {scene['roi_cloud_shadow_pct']:.2f}% theo SCL.")
            if (scene.get('band_coverage_pct') or 0) < 99.9:
                warnings.append(f"{scene['date']}: cảnh có thể thiếu dữ liệu, giữ nguyên NoData.")
        quality = None
        ready = True
        if merging:
            from .frame_validity import measure_frame_selection
            quality = FrameSelectionQuality.model_validate(measure_frame_selection(
                selected, params, deadline=time.monotonic()+120,
                work_directory=self.store.root / 'mask_staging')).model_dump()
            ready = quality['valid_pixel_count'] > 0 and quality['band_coverage_pct'] >= effective_min_coverage(params)
        immutable = {'search_id': str(search_id), 'parameters': params, 'scenes': selected,
                     'preflight_version': 2, 'can_start': ready,
                     'blocking_reason': None if ready else 'insufficient_selection_coverage',
                     'selection_quality': quality}
        data = {**immutable, 'digest': _digest(preflight_digest_payload(immutable)), 'output_grid': grid,
                'estimate': {'pixel_count_per_scene': grid['width'] * grid['height'],
                             'output_count': output_count, 'source_count': len(selected),
                             'uncompressed_bytes': total_bytes, **resources},
                'warnings': warnings, 'expires_at': time.time() + 86400}
        return self.store.save_preflight(data)

    def start_job(self, preflight_id, digest, acknowledged):
        with self._lock:
            if self._closed:
                raise FrameError('service_closed', 'Dịch vụ đang tắt; hãy thử lại.', 409)
            preflight = self.store.get_preflight(preflight_id)
            validate_preflight(preflight)
            if acknowledged is not True or digest != preflight['digest']:
                raise FrameError('stale_preflight', 'Xác nhận không khớp; hãy kiểm tra dung lượng lại.', 409)
            if preflight['expires_at'] < time.time():
                raise FrameError('stale_preflight', 'Xác nhận đã hết hạn; hãy kiểm tra lại.', 409)
            self._space(preflight['estimate']['disk_reserve_bytes'])
            job = self.store.create_job(preflight)
            key = job['job_id']
            if job['status'] == 'queued' and key not in self._futures:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='matched-frame')
                try:
                    self._futures[key] = self._executor.submit(self.run_job, key)
                except Exception:
                    self.store.update_job(key, status='failed', stage='failed', error='Không khởi động được tác vụ.')
                    raise FrameError('worker_failed', 'Không khởi động được tác vụ.', 502) from None
            return self.store.get_job(key)

    def run_job(self, key):
        from .frame_export import export_frame_scene, build_frame_manifest
        if not self.store.claim(key):
            return
        job = self.store.get_job(key)
        directory = self.store.job_dir(key)
        records = job['scenes']
        try:
            preflight = self.store.get_preflight(job['preflight_id'])
            parameters = preflight['parameters']
            validate_preflight(preflight)
            validate_job_selection(job, preflight)
            directory.mkdir(parents=True, exist_ok=True)
            from .frame_projection import estimate_export_resources
            reserve = estimate_export_resources(parameters['output_grid'], parameters['dataset'],
                                                parameters['bands'], len(preflight['scenes']))['disk_reserve_bytes']
            fatal = False
            for record in records:
                if self.store.finish_if_cancelled(key):
                    return
                token = str(uuid.uuid4())
                destination = directory / f'{token}.tif'
                record['status'] = 'running'
                self.store.update_job(key, stage='export', scenes=records)
                try:
                    if fatal:
                        raise FrameError('disk_space', 'Đã dừng do không đủ đĩa.')
                    self._space(max(reserve, preflight['estimate']['disk_reserve_bytes']))
                    report = export_frame_scene(record, parameters, destination)
                    if not destination.is_file():
                        raise FrameError('invalid_export', 'Tệp xuất không tồn tại.')
                    identity = '|'.join(s['asset_id'] for s in record.get('sources') or [record])
                    kind = 'mosaic_' if record.get('sources') else ''
                    dates = sorted(s['date'] for s in record.get('sources') or [record])
                    date_label = dates[0] if dates[0] == dates[-1] else dates[0] + '_' + dates[-1]
                    filename = (f"{parameters['dataset']}_{kind}{date_label}_"
                                f"{hashlib.sha256(identity.encode()).hexdigest()[:12]}_"
                                f"{parameters['grid_mode']}.tif")
                    record.update(status='succeeded', report=_safe_report(report), file_token=token,
                                  filename=filename, error=None)
                except Exception as exc:
                    destination.unlink(missing_ok=True)
                    if getattr(exc, 'errno', None) == 28 or isinstance(exc, FrameError) and exc.code in ('disk_space', 'disk_full'):
                        fatal = True
                    record.update(status='failed', error={'code': getattr(exc, 'code', 'export_failed'),
                                  'message': exc.message if isinstance(exc, FrameError) else 'Không xuất được cảnh; thử lại sau.'})
                    report = getattr(exc, 'report', None)
                    if report is not None:
                        record['report'] = _safe_report(report)
                succeeded = sum(r['status'] == 'succeeded' for r in records)
                failed = sum(r['status'] == 'failed' for r in records)
                self.store.update_job(key, scenes=records, processed=succeeded + failed, succeeded=succeeded, failed=failed)
                if self.store.finish_if_cancelled(key):
                    return
            manifest_reports = []
            quality_names = ('band_coverage_pct', 'roi_cloud_pct', 'roi_shadow_pct', 'roi_cloud_shadow_pct',
                             'roi_snow_pct', 'scl_valid_pct', 'tile_cloud_pct', 'covers_roi')
            for record in records:
                report = {**(record.get('report') or {}),
                          **{k: record.get(k) for k in ('scene_token', 'asset_id', 'timestamp', 'date', 'platform', 'status', 'filename')},
                          'quality': {k: record.get(k) for k in quality_names},
                          'estimated_band_coverage_pct': record.get('band_coverage_pct')}
                if record.get('error'):
                    report['error_code'] = record['error']['code']
                manifest_reports.append(report)
            manifest = build_frame_manifest(parameters, manifest_reports)
            manifest = _safe_report(manifest)
            manifest_path = directory / 'manifest.json'
            temp_manifest = directory / 'manifest.partial'
            temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
            os.replace(temp_manifest, manifest_path)
            succeeded = sum(r['status'] == 'succeeded' for r in records)
            if succeeded:
                if self.store.finish_if_cancelled(key):
                    return
                self.store.update_job(key, stage='archive')
                archive_bytes = sum(p.stat().st_size for p in directory.glob('*.tif'))
                self._space(max(MIN_FREE_BYTES, archive_bytes * 2))
                temporary = directory / 'result.partial'
                with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    archive.write(manifest_path, 'manifest.json')
                    for record in records:
                        if self.store.finish_if_cancelled(key):
                            return
                        if record['status'] == 'succeeded':
                            archive.write(directory / f"{record['file_token']}.tif", record['filename'])
                os.replace(temporary, directory / 'result.zip')
            status = 'completed' if succeeded == len(records) else ('partial' if succeeded else 'failed')
            self.store.finish_job(key, status)
        except Exception as exc:
            if not self.store.finish_if_cancelled(key):
                self.store.update_job(key, status='failed', stage='failed',
                                      error_code=getattr(exc, 'code', 'worker_failed'),
                                      error=exc.message if isinstance(exc, FrameError) else 'Tác vụ không hoàn tất; thử lại sau.')
        finally:
            for path in directory.glob('*.partial*'):
                path.unlink(missing_ok=True)

    def recover(self):
        return self.store.recover()

    def cleanup(self):
        return self.store.cleanup()

    def shutdown(self):
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True)
