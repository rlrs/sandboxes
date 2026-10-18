from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.resources import files


@dataclass(frozen=True)
class DashboardAsset:
    body: bytes
    content_type: str


_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard": ("index.html", "text/html; charset=utf-8"),
    "/dashboard/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}


@cache
def _read(name: str) -> bytes:
    return files(__name__).joinpath(name).read_bytes()


def dashboard_asset(path: str) -> DashboardAsset | None:
    asset = _ASSETS.get(path)
    if asset is None:
        return None
    name, content_type = asset
    return DashboardAsset(_read(name), content_type)
