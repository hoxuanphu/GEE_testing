"""Dataset-specific API platform codes and Earth Engine property filters."""
import ee


_PLATFORMS = {
    's1gee': ('platform_number', {code: code for code in 'ABCD'}),
    'sentinel2': ('SPACECRAFT_NAME', {code: 'Sentinel-2' + code for code in 'ABC'}),
}


def validate_platform(dataset: str, platform: str | None) -> str | None:
    """Only null means all; never broaden an invalid explicit selection."""
    if platform is None:
        return None
    if dataset not in _PLATFORMS:
        raise ValueError(f'Platform filtering is not supported for dataset {dataset}')
    if not isinstance(platform, str) or platform not in _PLATFORMS[dataset][1]:
        allowed = '/'.join(_PLATFORMS[dataset][1])
        raise ValueError(f'platform must be null or {allowed} for {dataset}')
    return platform


def apply_platform_filter(collection, dataset: str, platform: str | None):
    platform = validate_platform(dataset, platform)
    if platform is None:
        return collection
    property_name, values = _PLATFORMS[dataset]
    return collection.filter(ee.Filter.eq(property_name, values[platform]))
