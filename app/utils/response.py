"""JSON response helpers that keep API envelopes consistent."""

from __future__ import annotations

from typing import Any, Mapping

from fastapi.responses import JSONResponse


def success_response(
    data: Any = None,
    *,
    status_code: int = 200,
    meta: Mapping[str, Any] | None = None,
    status: str = "success",
) -> JSONResponse:
    """Return a standard success envelope: ``{"status": status, "data": ...}``.

    ``status`` can be overridden for non-200 acknowledgements, such as
    ``"accepted"`` for a 202 webhook response.
    """

    payload: dict[str, Any] = {
        "status": status,
        "data": {} if data is None else data,
    }
    if meta is not None:
        payload["meta"] = dict(meta)
    return JSONResponse(status_code=status_code, content=payload)


def error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    details: Any = None,
    request_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Return a standard error envelope with an optional correlation ID."""

    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    if request_id:
        error["request_id"] = request_id

    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "error": error},
        headers=dict(headers) if headers is not None else None,
    )
