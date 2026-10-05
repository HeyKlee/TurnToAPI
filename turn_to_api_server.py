from __future__ import annotations

import argparse
import asyncio
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from arena_model_registry import list_models
from browser_web_adapter import BrowserWebAdapter, BrowserResult, load_config


ROOT = Path(__file__).resolve().parent

app = FastAPI(title="TurnToAPI", version="0.1.0")

CONFIG: dict[str, Any] = {}
STARTED_AT = time.time()


def _model_cards() -> list[dict[str, Any]]:
    now = int(time.time())
    cards: list[dict[str, Any]] = []

    for mode in ("text", "code"):
        for item in list_models(mode):
            cards.append(
                {
                    "id": f"arena:{mode}:{item['id']}",
                    "object": "model",
                    "created": now,
                    "owned_by": "arena",
                }
            )

    if bool((CONFIG.get("chatgpt") or {}).get("enabled", False)):
        cards.append(
            {
                "id": "chatgpt:mode:web",
                "object": "model",
                "created": now,
                "owned_by": "chatgpt",
            }
        )

    return cards


def _resolve_adapter(model_id: str) -> BrowserWebAdapter:
    model_id = str(model_id or "").strip()

    if model_id.startswith("arena:text:"):
        model = model_id[len("arena:text:") :].strip()
        if not model:
            raise ValueError("Arena text model ID is empty.")
        return BrowserWebAdapter(
            provider="arena",
            mode="text",
            model=model,
            config=CONFIG,
        )

    if model_id.startswith("arena:code:"):
        model = model_id[len("arena:code:") :].strip()
        if not model:
            raise ValueError("Arena code model ID is empty.")
        return BrowserWebAdapter(
            provider="arena",
            mode="code",
            model=model,
            config=CONFIG,
        )

    if model_id == "chatgpt:mode:web":
        return BrowserWebAdapter(
            provider="chatgpt",
            mode="web",
            config=CONFIG,
        )

    raise ValueError(f"Unknown TurnToAPI model '{model_id}'.")


def _chunk(
    completion_id: str,
    model: str,
    *,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _completion_payload(
    completion_id: str,
    model: str,
    result: BrowserResult,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.content,
                    "reasoning_content": result.reasoning_content or None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "online",
        "service": "TurnToAPI",
        "uptime_seconds": int(time.time() - STARTED_AT),
        "browser_launch": "lazy",
    }


@app.get("/v1/models")
def models() -> dict[str, Any]:
    return {"object": "list", "data": _model_cards()}


@app.get("/__turntoapi/live-status")
def live_status() -> dict[str, Any]:
    return {
        "live_proxy": False,
        "integrated_streaming": True,
        "arena_browser": "playwright-firefox-persistent",
        "browser_launch": "on-demand-only",
        "security_challenges": "human-in-loop",
        "challenge_text_forwarded": False,
        "models": len(_model_cards()),
    }


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict[str, Any]):
    model = str(payload.get("model") or "").strip()
    messages = payload.get("messages") or []
    stream = bool(payload.get("stream", False))

    if not model:
        raise HTTPException(status_code=400, detail="model is required.")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(
            status_code=400,
            detail="messages must be a non-empty list.",
        )

    try:
        adapter = _resolve_adapter(model)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    completion_id = "chatcmpl-" + uuid.uuid4().hex

    if not stream:
        try:
            result = await asyncio.to_thread(adapter.complete, messages)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return JSONResponse(_completion_payload(completion_id, model, result))

    def event_stream() -> Iterator[str]:
        events: queue.Queue[Any] = queue.Queue()
        finished = threading.Event()

        def callback(reasoning: str, content: str) -> None:
            events.put(("snapshot", reasoning, content))

        def run() -> None:
            try:
                result = adapter.complete(messages, callback=callback)
                events.put(("done", result))
            except BaseException as exc:
                events.put(("error", exc))
            finally:
                finished.set()

        threading.Thread(
            target=run,
            name="TurnToAPI-Completion",
            daemon=True,
        ).start()

        yield _chunk(
            completion_id,
            model,
            delta={"role": "assistant"},
        )

        previous_reasoning = ""
        previous_content = ""

        while True:
            try:
                event = events.get(timeout=0.25)
            except queue.Empty:
                if finished.is_set():
                    break
                yield ": keep-alive\n\n"
                continue

            kind = event[0]

            if kind == "snapshot":
                reasoning = str(event[1] or "")
                content = str(event[2] or "")

                reasoning_add = (
                    reasoning[len(previous_reasoning) :]
                    if reasoning.startswith(previous_reasoning)
                    else reasoning
                )
                content_add = (
                    content[len(previous_content) :]
                    if content.startswith(previous_content)
                    else content
                )

                if reasoning_add:
                    yield _chunk(
                        completion_id,
                        model,
                        delta={"reasoning_content": reasoning_add},
                    )
                if content_add:
                    yield _chunk(
                        completion_id,
                        model,
                        delta={"content": content_add},
                    )

                previous_reasoning = reasoning
                previous_content = content
                continue

            if kind == "done":
                result: BrowserResult = event[1]

                reasoning_add = (
                    result.reasoning_content[len(previous_reasoning) :]
                    if result.reasoning_content.startswith(previous_reasoning)
                    else result.reasoning_content
                )
                content_add = (
                    result.content[len(previous_content) :]
                    if result.content.startswith(previous_content)
                    else result.content
                )

                if reasoning_add:
                    yield _chunk(
                        completion_id,
                        model,
                        delta={"reasoning_content": reasoning_add},
                    )
                if content_add:
                    yield _chunk(
                        completion_id,
                        model,
                        delta={"content": content_add},
                    )

                yield _chunk(
                    completion_id,
                    model,
                    delta={},
                    finish_reason="stop",
                )
                yield "data: [DONE]\n\n"
                return

            if kind == "error":
                exc = event[1]
                yield "data: " + json.dumps(
                    {
                        "error": {
                            "message": f"{type(exc).__name__}: {exc}",
                            "type": "browser_adapter_error",
                        }
                    }
                ) + "\n\n"
                yield "data: [DONE]\n\n"
                return

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def main() -> None:
    global CONFIG

    parser = argparse.ArgumentParser(
        description="TurnToAPI OpenAI-compatible server"
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.yaml"),
    )
    args = parser.parse_args()

    CONFIG = load_config(args.config)

    host = str(args.host or CONFIG.get("host") or "127.0.0.1")
    port = int(args.port or CONFIG.get("port") or 8000)

    allow_public = bool(
        (CONFIG.get("security") or {}).get("allow_public_bind", False)
    )
    if host in {"0.0.0.0", "::"} and not allow_public:
        raise SystemExit(
            "TurnToAPI refuses to bind publicly. Use 127.0.0.1, a Tailscale "
            "address, or explicitly set security.allow_public_bind=true."
        )

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
