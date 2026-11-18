"""Versioned values shared by frozen selections and resumable exports."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

PIPELINE_VERSION = 's2-raw-binary-owner-v1'
LAYOUT_VERSION = 'legacy-wgs84-single-band-8m-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


@dataclass
class SelectionTrace:
    assets: list[str] = field(default_factory=list)
    priority: list[int] = field(default_factory=list)
    layers: list[list[int]] = field(default_factory=list)
    contributors: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class FrozenSelection:
    token: str
    selection_hash: str
    request: dict
    image_graph: str
    recipe: dict
    scene: dict
    provider_scope: str
    version: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class TileSpec:
    tile_key: str
    selection_hash: str
    band: str
    region: tuple[float, float, float, float]
    scale: float
    crs: str = 'EPSG:4326'
    layout: str = LAYOUT_VERSION
    max_pixels: int = 8_100_000


@dataclass(frozen=True)
class TileReceipt:
    tile_key: str
    filename: str
    sha256: str
    size: int
    schema: dict


@dataclass(frozen=True)
class Attempt:
    artifact_key: str
    number: int
    owner: str


@dataclass(frozen=True)
class DownloadSettings:
    workers: int = 6
    backlog: int = 12
    compression_threads: int = 2
    gdal_cache_bytes: int = 256 * 1024 * 1024
    vrt: bool = False
    writer_version: int = 2

    def __post_init__(self):
        if (not 1 <= self.workers <= 6 or not self.workers <= self.backlog <= 12 or
            not 1 <= self.compression_threads <= 4 or not 64*1024*1024 <= self.gdal_cache_bytes <= 512*1024*1024
            or self.writer_version!=2):
            raise ValueError('invalid_download_profile')

    def disk_reserve(self, logical_bytes, vrt=None):
        return (2 if (self.vrt if vrt is None else vrt) else 3)*logical_bytes+2*1024**3

    def writer_options(self):
        return dict(compress='deflate',BIGTIFF='YES',interleave='band',block_size=256,
                    compression_threads=self.compression_threads,vrt=self.vrt,writer_version=self.writer_version)
