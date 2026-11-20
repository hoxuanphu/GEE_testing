"""Application-owned contracts for the optional catalogue backend."""
from dataclasses import dataclass, field
from typing import Protocol, Any


@dataclass
class CatalogResult:
    scenes: list[dict]
    parameters: dict = field(default_factory=dict)


class CatalogUnavailable(Exception):
    """The optional backend is disabled or cannot be configured."""


class CatalogFailure(Exception):
    """The selected provider failed before returning a usable page."""


class CatalogGateway(Protocol):
    last_page: Any

    def search(self, **kwargs: Any) -> Any: ...
