"""Google Earth Engine session initialization (service account)."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import ee

from .provider_credentials import active_credentials

_BASE_DIR = Path(__file__).resolve().parent.parent

_initialized = False


def _load_dotenv() -> None:
    """Load .env from project root into environment (no dependency needed)."""
    env_path = _BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _load_extra_cas() -> None:
    """Trust corporate proxy CA certificates (SSL interception) for requests.

    Combines the exported corporate CA bundle (vingroup_cas.pem) with certifi's
    system CAs and points REQUESTS_CA_BUNDLE at it. Must run before ee/requests
    build their sessions.
    """
    ca_path = _BASE_DIR / "vingroup_cas.pem"
    if not ca_path.exists():
        return
    combined = ca_path.read_text(encoding="ascii")
    try:
        import certifi

        combined += certifi.contents()
    except Exception:
        pass
    combined_path = _BASE_DIR / "combined_cas.pem"
    try:
        current = combined_path.read_text(encoding="ascii")
    except FileNotFoundError:
        current = None
    if current != combined:
        with NamedTemporaryFile(mode='w', encoding='ascii', prefix='combined_cas-',
                                suffix='.tmp', dir=_BASE_DIR, delete=False) as file:
            temporary = Path(file.name)
            file.write(combined)
        try:
            os.replace(temporary, combined_path)
        finally:
            temporary.unlink(missing_ok=True)
    os.environ["REQUESTS_CA_BUNDLE"] = str(combined_path)


def init_ee() -> None:
    """Initialize Earth Engine once, using a service account.

    Priority:
    1. Service account saved in the local provider settings.
    2. GEE_SERVICE_ACCOUNT + GEE_KEY_FILE (default file: ee_private_key.json).
    3. Existing Earth Engine CLI credentials.
    """
    global _initialized
    if _initialized:
        return

    _load_dotenv()
    _load_extra_cas()
    saved = active_credentials('gee')
    if saved:
        credentials = ee.ServiceAccountCredentials(key_data=json.dumps(saved))
        ee.Initialize(credentials, project=saved['project_id'])
        _initialized = True
        return
    service_account = os.getenv("GEE_SERVICE_ACCOUNT", "").strip()
    key_file = os.getenv("GEE_KEY_FILE", "ee_private_key.json").strip()
    key_path = _BASE_DIR / key_file

    if service_account and key_path.exists():
        credentials = ee.ServiceAccountCredentials(service_account, str(key_path))
        ee.Initialize(credentials)
    else:
        # Fall back to any credentials already available (e.g. previously
        # authenticated user with `earthengine authenticate`).
        ee.Initialize()

    _initialized = True
