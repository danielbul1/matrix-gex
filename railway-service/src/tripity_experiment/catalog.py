"""A small catalog of real public APIs, so users connect by name — no links.

Each entry maps a friendly name to a real OpenAPI spec, base URL, and a safe
default set of operations to expose. This is the seed of the "shelf" the user
browses; it can later be backed by the APIs.guru directory (2500+ APIs) for
scale.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    description: str
    openapi_url: str
    base_url: str
    operations: tuple[str, ...]


CURATED: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name="petstore",
        description="Swagger Petstore demo — look up pets by id.",
        openapi_url="https://petstore3.swagger.io/api/v3/openapi.json",
        base_url="https://petstore3.swagger.io/api/v3",
        operations=("getPetById",),
    ),
    CatalogEntry(
        name="apis-guru",
        description="APIs.guru — directory of thousands of public APIs.",
        openapi_url="https://api.apis.guru/v2/specs/apis.guru/2.2.0/openapi.json",
        base_url="https://api.apis.guru/v2",
        operations=("getMetrics", "listAPIs"),
    ),
)


def find_entry(name: str, entries: Iterable[CatalogEntry] = CURATED) -> CatalogEntry | None:
    key = name.strip().casefold()
    for entry in entries:
        if entry.name.casefold() == key:
            return entry
    return None
