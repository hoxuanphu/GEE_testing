"""Contracts and resource limits for reference-frame downloads."""
from __future__ import annotations

from datetime import date
import math
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024
MAX_DIMENSION = 30000
MAX_OUTPUT_PIXELS = 100000000
MAX_SCENE_COUNT = 10
MAX_JOB_BYTES = 4 * 1024 ** 3
MIN_FREE_BYTES = 512 * 1024 ** 2
TTL_SECONDS = 86400
S2_BANDS = ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B8A', 'B9', 'B11', 'B12']
S2_PRESETS = {'full': S2_BANDS, 'rgb': ['B4', 'B3', 'B2'], 'rgbnir': ['B2', 'B3', 'B4', 'B8']}
S1_POLARIZATIONS = {'vvvh': ['VV', 'VH'], 'vv': ['VV'], 'hhhv': ['HH', 'HV'], 'hh': ['HH']}


class FrameError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def effective_min_coverage(parameters) -> float:
    """Resolve the actual output threshold, preserving an explicit zero."""
    get = parameters.get if isinstance(parameters, dict) else lambda key, default=None: getattr(parameters, key, default)
    minimum = get('min_coverage_pct', 0)
    if (isinstance(minimum, bool) or not isinstance(minimum, (int, float))
            or not math.isfinite(minimum) or not 0 <= minimum <= 100):
        raise FrameError('invalid_coverage', 'Ngưỡng độ phủ phải là số hữu hạn từ 0 đến 100.')
    return 99.9 if get('require_full_coverage', True) else float(minimum)


def known_positive_coverage(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 < value <= 100)


class FrameContract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)


class FrameSelectionQuality(FrameContract):
    measurement_version: Literal[1]
    method: Literal['output_grid_mask']
    estimated: Literal[True]
    band_coverage_pct: float = Field(ge=0, le=100, strict=True)
    valid_pixel_count: int = Field(ge=0, strict=True)
    total_pixels: int = Field(gt=0, le=MAX_OUTPUT_PIXELS, strict=True)
    ordered_asset_ids: list[str] = Field(min_length=1, max_length=MAX_SCENE_COUNT)
    roi_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    merge_policy: Literal['primary_first_fill_nodata']

    @model_validator(mode='before')
    @classmethod
    def strict_identity_fields(cls, value):
        if isinstance(value, dict) and (
                type(value.get('measurement_version')) is not int or type(value.get('estimated')) is not bool):
            raise ValueError('Phiên bản và cờ ước lượng mask không hợp lệ.')
        return value

    @model_validator(mode='after')
    def consistent_counts(self):
        if (self.valid_pixel_count > self.total_pixels or
                abs(self.band_coverage_pct - self.valid_pixel_count * 100.0 / self.total_pixels) > 1e-8):
            raise ValueError('Độ phủ không khớp số pixel hợp lệ.')
        return self


class FrameGrid(FrameContract):
    crs: str = Field(min_length=1, max_length=16384)
    transform: tuple[float, float, float, float, float, float]
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)

    @model_validator(mode='after')
    def valid_grid(self):
        a, b, _, d, e, _ = self.transform
        if not all(math.isfinite(v) for v in self.transform) or a * e - b * d == 0:
            raise ValueError('Affine không hợp lệ hoặc suy biến.')
        return self


class ReferenceFrame(FrameGrid):
    display_name: str
    count: int = Field(gt=0)
    descriptions: list[str | None]
    dtypes: list[str]
    tags: dict = Field(default_factory=dict)
    corners: list[list[float]]
    bounds: list[float]
    footprint_geojson: dict
    compatible_grid_modes: list[str]
    pixel_size: list[float]
    pixel_unit: str


class FrameSearchRequest(FrameContract):
    reference_id: UUID
    dataset: Literal['sentinel2', 's1gee'] = 'sentinel2'
    selection_context: Literal['reference', 'reference_roi'] = 'reference'
    start_date: date
    end_date: date
    grid_mode: Literal['match_extent', 'match_grid'] = 'match_extent'
    target_resolution_m: Literal[10, 20, 30, 60] = 10
    band_preset: Literal['full', 'rgb', 'rgbnir'] = 'full'
    polarization: Literal['vvvh', 'vv', 'hhhv', 'hh'] = 'vvvh'
    sort: Literal['least_cloud', 'nearest_date'] = 'least_cloud'
    reference_date: date | None = None
    require_full_coverage: bool = True
    max_cloud_shadow_pct: float | None = Field(None, ge=0, le=100)
    min_cloud_shadow_pct: float | None = Field(None, ge=0, le=100)
    min_coverage_pct: float = Field(99.9, ge=0, le=100)
    merge_scenes: bool = False
    orbit_pass: Literal['ASCENDING', 'DESCENDING'] | None = None
    relative_orbit: int | None = Field(None, ge=1, le=175)
    platform: Literal['A', 'B', 'C', 'D'] | None = None
    prefer_reference_orbit: bool = False
    reference_orbit_pass: Literal['ASCENDING', 'DESCENDING'] | None = None
    reference_relative_orbit: int | None = Field(None, ge=1, le=175)

    @model_validator(mode='after')
    def valid_search(self):
        from .platforms import validate_platform
        validate_platform(self.dataset, self.platform)
        if self.end_date < self.start_date or (self.end_date - self.start_date).days >= 366:
            raise ValueError('Khoảng ngày phải từ 1 đến 366 ngày.')
        if self.end_date == date.max:
            raise ValueError('Ngày kết thúc không được là 9999-12-31.')
        if (self.min_cloud_shadow_pct is not None and self.max_cloud_shadow_pct is not None
                and self.min_cloud_shadow_pct > self.max_cloud_shadow_pct):
            raise ValueError('Mây tối thiểu không được lớn hơn mây tối đa.')
        if self.sort == 'nearest_date' and self.reference_date is None:
            raise ValueError('Cần ngày tham chiếu khi xếp theo ngày gần nhất.')
        if self.prefer_reference_orbit and (self.dataset != 's1gee' or
                self.reference_orbit_pass is None or self.reference_relative_orbit is None):
            raise ValueError('Ưu tiên quỹ đạo cần hướng bay và quỹ đạo tham chiếu đã xác nhận.')
        return self


class FrameScene(FrameContract):
    scene_token: UUID
    asset_id: str
    timestamp: str
    date: str
    platform: str | None = None
    tile: str | None = None
    orbit_pass: str | None = None
    relative_orbit: int | None = None
    covers_roi: bool = False
    footprint: dict | None = None
    band_coverage_pct: float | None = Field(None, ge=0, le=100)
    roi_cloud_pct: float | None = None
    roi_shadow_pct: float | None = None
    roi_cloud_shadow_pct: float | None = None
    roi_snow_pct: float | None = None
    scl_valid_pct: float | None = None
    tile_cloud_pct: float | None = None
    eligible: bool = True
    warnings: list[str] = Field(default_factory=list)
    quality_context: dict = Field(default_factory=dict)
    reference_orbit_match: bool = False


class FramePreflightRequest(FrameContract):
    search_id: UUID
    scene_tokens: list[UUID] = Field(min_length=1, max_length=10)

    @model_validator(mode='after')
    def unique_scenes(self):
        if len(set(self.scene_tokens)) != len(self.scene_tokens):
            raise ValueError('Không được chọn trùng cảnh.')
        return self


class FrameJobRequest(FrameContract):
    preflight_id: UUID
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    acknowledged: Literal[True]


def bands_for(request) -> list[str]:
    if isinstance(request, dict):
        dataset, preset, polarization = request['dataset'], request.get('band_preset', 'full'), request.get('polarization', 'vvvh')
    else:
        dataset, preset, polarization = request.dataset, request.band_preset, request.polarization
    return list(S2_PRESETS[preset] if dataset == 'sentinel2' else S1_POLARIZATIONS[polarization])
