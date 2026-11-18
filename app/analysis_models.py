"""Versioned, bounded analysis requests (GeoJSON coordinates are WGS84)."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Scene(StrictModel):
    source: Literal['sentinel2', 'landsat', 'sentinel1', 'uav']
    date: date
    end_date: date | None = None
    upload_id: str | None = Field(None, pattern=r'^[a-f0-9]{32}$')
    bands: dict[str, int] = Field(default_factory=dict)
    scale: float = 1
    offset: float = 0
    orbit_pass: Literal['ASCENDING', 'DESCENDING'] | None = None
    relative_orbit: int | None = Field(None, ge=1, le=175)

    @model_validator(mode='after')
    def check(self):
        if self.end_date and self.end_date <= self.date:
            raise ValueError('end_date must follow date (exclusive)')
        if self.source == 'uav' and (not self.upload_id or not self.bands):
            raise ValueError('UAV requires upload_id and explicit bands mapping')
        if any(k not in {'blue','green','red','nir','swir1','swir2','VV','VH','slope','hand','distance'} or v < 1 or v > 32 for k,v in self.bands.items()):
            raise ValueError('Unsupported band name or band number')
        if self.source == 'sentinel1' and (not self.orbit_pass or not self.relative_orbit):
            raise ValueError('SAR requires explicit orbit_pass and relative_orbit')
        return self


class Rules(StrictModel):
    ndvi_drop: float = Field(.2, gt=0)
    bsi_rise: float = Field(.1, gt=0)
    ndmi_drop: float = Field(.1, gt=0)
    water_min: float = .2
    sar_drop_db: float = Field(3, gt=0)
    min_evidence: int = Field(2, ge=1, le=5)
    min_area_m2: float = Field(100, ge=0)
    max_slope: float | None = Field(None, ge=0, le=90)
    max_hand: float | None = Field(None, ge=0)
    max_distance: float | None = Field(None, ge=0)


class AnalysisRequest(StrictModel):
    schema_version: Literal['1.0'] = '1.0'
    mode: Literal['indices','change','timeseries','flood'] = 'indices'
    roi: dict
    resolution: float = Field(10, ge=1, le=1000)
    indices: list[Literal['NDVI','EVI','SAVI','NDMI','BSI','MNDWI','VV','VH']] = Field(default_factory=lambda:['NDVI'], min_length=1,max_length=8)
    scenes: list[Scene] = Field(min_length=1,max_length=24)
    rules: Rules = Field(default_factory=Rules)

    @model_validator(mode='after')
    def check(self):
        from .uav_ingestion import validate_geometry
        validate_geometry(self.roi)
        if len(set(self.indices)) != len(self.indices):
            raise ValueError('Duplicate indices')
        if self.mode in ('change','flood'):
            if len(self.scenes) != 2 or self.scenes[0].date >= self.scenes[1].date:
                raise ValueError('Change/flood requires two chronological scenes')
            a,b = self.scenes
            if a.source != b.source:
                raise ValueError('Change requires comparable source types')
            if a.source == 'sentinel1' and (a.orbit_pass,a.relative_orbit)!=(b.orbit_pass,b.relative_orbit):
                raise ValueError('SAR pair orbit mismatch')
        if self.mode == 'indices' and len(self.scenes) != 1:
            raise ValueError('Indices mode requires one scene')
        return self
