"""Local developer dashboard: static page plus read-only support endpoints.

The dashboard has no authentication. It is served from the same origin as the
JSON API, so no CORS changes are involved. Only two small data endpoints are
added here; every memory, conflict, search, and explain action uses the
existing product API.

Report files are read-only and constrained to ``settings.eval_reports_path``.
The runtime endpoint exposes provider labels and feature flags, never keys,
base URLs, or the database path.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT

DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
ASSETS_DIR = DASHBOARD_DIR / "assets"
MAX_REPORT_BYTES = 5 * 1024 * 1024
MAX_REPORTS_LISTED = 50

_REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

router = APIRouter(prefix="/dashboard", include_in_schema=False)


class DashboardStaticFiles(StaticFiles):
    """Static assets with no-store, so a reload never serves stale JS."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


@router.get("")
def dashboard_root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/")
def dashboard_index() -> Response:
    index = DASHBOARD_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="dashboard is not installed")
    return Response(
        content=index.read_text(encoding="utf-8"),
        media_type="text/html",
        headers=_SECURITY_HEADERS,
    )


@router.get("/api/runtime")
def dashboard_runtime(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    embedding = getattr(request.app.state, "embedding_provider", None)
    llm = getattr(request.app.state, "llm_provider", None)
    return {
        "app": {
            "name": request.app.title,
            "version": request.app.version,
            "environment": settings.environment,
        },
        "embedding": {
            "provider": settings.embedding_provider,
            "model": embedding.model if embedding is not None else None,
            "dimensions": embedding.dimensions if embedding is not None else None,
            "external": embedding is not None and settings.embedding_provider != "fake",
        },
        "llm": {
            "provider": settings.llm_provider,
            "model": llm.model if llm is not None else None,
        },
        "features": {
            "store_messages": settings.store_messages,
            "conflict_detection": settings.conflict_detection_enabled,
        },
        "notice": ("Local developer tool. No authentication. user_id is caller-asserted."),
    }


def _reports_dir(request: Request) -> Path:
    return Path(request.app.state.settings.eval_reports_path).resolve()


def _report_summary(payload: object) -> dict[str, Any]:
    """Extract the small, read-only facts the Evaluation Lab needs to rank files.

    Everything here comes from the report itself. Nothing is inferred about
    quality, and an unrecognised shape simply yields empty values so the client
    can mark the file as incompatible instead of guessing.
    """
    if not isinstance(payload, dict):
        return {}

    dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    modes = payload.get("modes") if isinstance(payload.get("modes"), dict) else {}
    search_config = (
        payload.get("search_configuration")
        if isinstance(payload.get("search_configuration"), dict)
        else {}
    )
    provider = (
        search_config.get("provider") if isinstance(search_config.get("provider"), dict) else {}
    )

    per_query_counts = {}
    for name, result in modes.items():
        if isinstance(result, dict) and isinstance(result.get("per_query"), list):
            per_query_counts[name] = len(result["per_query"])

    dataset_queries = dataset.get("queries")
    min_evaluated = min(per_query_counts.values()) if per_query_counts else None
    all_modes_full = (
        bool(per_query_counts)
        and isinstance(dataset_queries, int)
        and dataset_queries > 1
        and all(count == dataset_queries for count in per_query_counts.values())
    )
    run_mode = run.get("mode")
    is_offline = run_mode == "offline" or provider.get("name") == "fake"

    return {
        "dataset_version": dataset.get("version"),
        "dataset_sha256": dataset.get("sha256"),
        "dataset_queries": dataset_queries if isinstance(dataset_queries, int) else None,
        "evaluated_queries": min_evaluated,
        "all_modes_full": all_modes_full,
        "modes": sorted(per_query_counts),
        "k_values": run.get("k_values") if isinstance(run.get("k_values"), list) else None,
        "run_mode": run_mode,
        "is_offline": is_offline,
        "report_version": payload.get("report_version"),
        "fingerprint": payload.get("deterministic_fingerprint"),
    }


@router.get("/api/reports")
def list_reports(request: Request) -> dict[str, Any]:
    directory = _reports_dir(request)
    if not directory.is_dir():
        return {"items": []}

    items = []
    for path in directory.glob("*.json"):
        if not path.is_file() or not _REPORT_NAME.match(path.name):
            continue
        stat = path.stat()
        item: dict[str, Any] = {
            "name": path.name,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            "readable": True,
            "unreadable_reason": None,
        }
        if stat.st_size > MAX_REPORT_BYTES:
            item["readable"] = False
            item["unreadable_reason"] = "too_large"
        else:
            try:
                item.update(_report_summary(json.loads(path.read_text(encoding="utf-8"))))
            except OSError, ValueError:
                # Listed, but the client must not try to render it.
                item["readable"] = False
                item["unreadable_reason"] = "invalid_json"
        items.append(item)

    items.sort(key=lambda item: item["modified_at"], reverse=True)
    return {"items": items[:MAX_REPORTS_LISTED]}


@router.get("/api/reports/{name}")
def read_report(name: str, request: Request) -> JSONResponse:
    if not _REPORT_NAME.match(name):
        raise HTTPException(status_code=404, detail="report not found")

    directory = _reports_dir(request)
    path = (directory / name).resolve()
    # Confirm the resolved path is a direct child of the configured directory,
    # which also rejects symlinks pointing outside it.
    if path.parent != directory or not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")

    if path.stat().st_size > MAX_REPORT_BYTES:
        raise HTTPException(status_code=413, detail="report is too large")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise HTTPException(status_code=422, detail="report is not valid JSON") from None
    return JSONResponse(content=payload)
