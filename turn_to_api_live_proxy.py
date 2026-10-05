from __future__ import annotations

import argparse

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse


app = FastAPI(title="TurnToAPI compatibility proxy")
BACKEND = "http://127.0.0.1:8001"
CLIENT: httpx.AsyncClient | None = None


@app.on_event("startup")
async def startup() -> None:
    global CLIENT
    CLIENT = httpx.AsyncClient(timeout=None, follow_redirects=False)


@app.on_event("shutdown")
async def shutdown() -> None:
    global CLIENT
    if CLIENT is not None:
        await CLIENT.aclose()
        CLIENT = None


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(request: Request, path: str):
    assert CLIENT is not None

    url = BACKEND + "/" + path
    if request.url.query:
        url += "?" + request.url.query

    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }

    upstream_request = CLIENT.build_request(
        request.method,
        url,
        headers=headers,
        content=body,
    )
    upstream = await CLIENT.send(upstream_request, stream=True)

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower()
        not in {
            "content-length",
            "transfer-encoding",
            "connection",
            "content-encoding",
        }
    }

    content_type = upstream.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        async def iterator():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            iterator(),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            headers=response_headers,
        )

    data = await upstream.aread()
    await upstream.aclose()
    return Response(
        content=data,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


def main() -> None:
    global BACKEND

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--backend", default="http://127.0.0.1:8001")
    args = parser.parse_args()

    BACKEND = args.backend.rstrip("/")

    if args.host in {"0.0.0.0", "::"}:
        raise SystemExit(
            "Compatibility proxy refuses a public wildcard bind. "
            "Use loopback or a Tailscale address."
        )

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
