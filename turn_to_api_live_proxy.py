import argparse
import asyncio
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil
import uvicorn

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


ROOT = Path(__file__).resolve().parent

BACKEND = "http://127.0.0.1:8001"

EVENT_PORT = 8765

ARENA_PROFILE = str(
    (
        ROOT
        / "firefox_profiles"
        / "arena"
    ).resolve()
).lower()


app = FastAPI(
    title="TurnToAPI Live Proxy"
)


ARENA_LOCK = asyncio.Lock()

EVENT_QUEUE = queue.Queue()

STATE_LOCK = threading.Lock()

ACTIVE_SESSION = ""

LAST_EVENT = {}

EVENT_COUNT = 0

EVENT_SERVER = None


HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}



# TURNTOAPI_ARENA_STREAM_SANITIZER_V1

import re as _turntoapi_re


_TURNTOAPI_ARENA_UI_FRAGMENTS = (
    "Battle Mode",
    "Battle 2 anonymous models",
    "Agent Mode",
    "Built for complex tasks",
    "Side by Side",
    "Compare 2 models of your choice",
    "Direct",
    "Chat with 1 model at a time",
    "Inputs are processed by third-party AI and responses may be inaccurate.",
)


_TURNTOAPI_SECURITY_MARKERS = (
    "Security Verification",
    "Please complete this quick security check to continue",
    "Protected by reCAPTCHA",
    "reCAPTCHA",
    "performing security verification",
    "verify you are human",
    "checking your browser",
    "just a moment",
)


def _turntoapi_is_security_page(
    value,
):

    lower = str(
        value
        or ""
    ).lower()

    return any(
        marker.lower()
        in lower

        for marker
        in _TURNTOAPI_SECURITY_MARKERS
    )


def _turntoapi_sanitize_arena_text(
    value,
):

    value = str(
        value
        or ""
    )

    if not value:
        return ""


    if _turntoapi_is_security_page(
        value
    ):
        return ""


    #
    # Remove exact Arena navigation / disclaimer fragments.
    #

    for fragment in (
        _TURNTOAPI_ARENA_UI_FRAGMENTS
    ):

        value = value.replace(
            fragment,
            ""
        )


    #
    # Handle the common fully-concatenated Arena mode bar.
    #

    value = _turntoapi_re.sub(
        (
            r"Battle\s*Mode"
            r".*?"
            r"Inputs\s+are\s+processed\s+by\s+third-party\s+AI"
            r"\s+and\s+responses\s+may\s+be\s+inaccurate\.?"
        ),
        "",
        value,
        flags=(
            _turntoapi_re.I
            |
            _turntoapi_re.S
        ),
    )


    #
    # Remove standalone UI labels that sometimes bleed into the
    # response card scrape.
    #

    ui_lines = {
        "Battle Mode",
        "Battle",
        "Agent Mode",
        "Side by Side",
        "Direct",
        "Chat completed",
        "Stop generating",
        "Generating...",
        "Building...",
        "Response provided by",
    }


    cleaned = []


    for raw in value.replace(
        "\r",
        ""
    ).split(
        "\n"
    ):

        line = raw.strip()


        if not line:
            continue


        if line in ui_lines:
            continue


        if line.startswith(
            "Thought for "
        ):

            #
            # The label belongs to Arena UI.
            # Actual reasoning text remains separate.
            #

            continue


        if line.startswith(
            "Preview will appear"
        ):
            continue


        cleaned.append(
            raw
        )


    value = "\n".join(
        cleaned
    ).strip()


    #
    # Final whitespace normalization without flattening paragraphs.
    #

    value = _turntoapi_re.sub(
        r"[ \t]{2,}",
        " ",
        value,
    )

    value = _turntoapi_re.sub(
        r"\n{3,}",
        "\n\n",
        value,
    )


    return value.strip()



def activate_session(
    session_id,
):
    global ACTIVE_SESSION
    global LAST_EVENT

    with STATE_LOCK:
        ACTIVE_SESSION = session_id
        LAST_EVENT = {}

    while True:
        try:
            EVENT_QUEUE.get_nowait()

        except queue.Empty:
            break


def deactivate_session(
    session_id,
):
    global ACTIVE_SESSION

    with STATE_LOCK:

        if ACTIVE_SESSION == session_id:
            ACTIVE_SESSION = ""


def snapshot(
    session_id,
):
    with STATE_LOCK:

        if ACTIVE_SESSION != session_id:
            return {}

        return dict(
            LAST_EVENT
        )


class EventHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        fmt,
        *args,
    ):
        return


    def add_headers(
        self,
    ):
        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET,POST,OPTIONS",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type",
        )

        self.send_header(
            "Access-Control-Allow-Private-Network",
            "true",
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )


    def do_OPTIONS(
        self,
    ):
        self.send_response(
            204
        )

        self.add_headers()

        self.end_headers()


    def do_GET(
        self,
    ):
        if self.path != "/session":

            self.send_response(
                404
            )

            self.end_headers()

            return

        with STATE_LOCK:
            value = ACTIVE_SESSION

        raw = value.encode(
            "utf-8"
        )

        self.send_response(
            200
        )

        self.add_headers()

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(
                len(raw)
            ),
        )

        self.end_headers()

        self.wfile.write(
            raw
        )


    def do_POST(
        self,
    ):
        global LAST_EVENT
        global EVENT_COUNT

        if self.path != "/event":

            self.send_response(
                404
            )

            self.end_headers()

            return

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
                or 0
            )

            raw = self.rfile.read(
                length
            )

            data = json.loads(
                raw.decode(
                    "utf-8",
                    "replace",
                )
            )

            sid = str(
                data.get(
                    "session",
                    "",
                )
            )

            with STATE_LOCK:

                active = ACTIVE_SESSION

                if (
                    sid
                    and sid == active
                ):

                    _raw_reasoning = str(
                        data.get(
                            "reasoning",
                            "",
                        )
                        or ""
                    )

                    _raw_content = str(
                        data.get(
                            "content",
                            "",
                        )
                        or ""
                    )

                    _security_block = (
                        _turntoapi_is_security_page(
                            _raw_reasoning
                        )
                        or
                        _turntoapi_is_security_page(
                            _raw_content
                        )
                    )

                    if _security_block:

                        _reasoning = ""
                        _content = ""

                    else:

                        _reasoning = (
                            _turntoapi_sanitize_arena_text(
                                _raw_reasoning
                            )
                        )

                        _content = (
                            _turntoapi_sanitize_arena_text(
                                _raw_content
                            )
                        )

                    LAST_EVENT = {
                        "session":
                            sid,

                        "reasoning":
                            _reasoning,

                        "content":
                            _content,

                        "security_block":
                            _security_block,

                        "ts":
                            time.time(),
                    }

                    EVENT_COUNT += 1

                    EVENT_QUEUE.put(
                        dict(
                            LAST_EVENT
                        )
                    )

            self.send_response(
                204
            )

            self.add_headers()

            self.end_headers()

        except Exception:

            self.send_response(
                400
            )

            self.add_headers()

            self.end_headers()


def start_event_server():
    global EVENT_SERVER

    EVENT_SERVER = ThreadingHTTPServer(
        (
            "127.0.0.1",
            EVENT_PORT,
        ),
        EventHandler,
    )

    thread = threading.Thread(
        target=EVENT_SERVER.serve_forever,
        daemon=True,
    )

    thread.start()


def firefox_janitor():

    # TURNTOAPI_FIREFOX_JANITOR_DISABLED_V1
    #
    # IMPORTANT:
    #
    # Firefox's multi-process startup can temporarily expose more
    # than one process associated with the same profile.
    #
    # Killing one based only on process age/profile membership can
    # terminate the exact Firefox root GeckoDriver owns, producing:
    #
    #   WebDriverException:
    #   Process (...) unexpectedly closed with status 15
    #
    # Therefore TurnToAPI performs NO background Firefox killing.
    #
    # Browser lifecycle belongs to WebDriver itself.
    #

    while True:

        time.sleep(
            3600
        )


@app.on_event(
    "startup"
)
def startup():

    try:

        start_event_server()

    except OSError:
        # Existing previous live proxy may still be releasing
        # the port. The installer removes it before startup.
        pass


    threading.Thread(
        target=firefox_janitor,
        daemon=True,
    ).start()


def forward_headers(
    request,
):
    return {
        k: v
        for k, v
        in request.headers.items()
        if k.lower()
        not in HOP_HEADERS
    }


def response_headers(
    response,
):
    return {
        k: v
        for k, v
        in response.headers.items()
        if k.lower()
        not in HOP_HEADERS
    }


def backend_url(
    path,
    request,
):
    value = (
        BACKEND.rstrip("/")
        + "/"
        + path
    )

    if request.url.query:

        value += (
            "?"
            + request.url.query
        )

    return value


async def call_backend_json(
    payload,
    headers,
):

    async with httpx.AsyncClient(
        timeout=None
    ) as client:

        return await client.post(
            (
                BACKEND.rstrip("/")
                + "/v1/chat/completions"
            ),
            json=payload,
            headers=headers,
        )


def chunk(
    model,
    delta,
    finish_reason=None,
):

    value = {
        "id":
            "chatcmpl-turntoapi-live",

        "object":
            "chat.completion.chunk",

        "created":
            int(
                time.time()
            ),

        "model":
            model,

        "choices": [
            {
                "index":
                    0,

                "delta":
                    delta,

                "finish_reason":
                    finish_reason,
            }
        ],
    }

    return (
        "data: "
        + json.dumps(
            value,
            ensure_ascii=False,
            separators=(
                ",",
                ":",
            ),
        )
        + "\n\n"
    )


def message_from_response(
    response,
):

    try:

        data = response.json()

        message = (
            data[
                "choices"
            ][0][
                "message"
            ]
        )

        return (
            data,
            message,
        )

    except Exception:

        return (
            None,
            None,
        )


def looks_polluted(
    value,
):

    lower = str(
        value
        or ""
    ).lower()

    markers = (
        "battle mode",
        "battle 2 anonymous models",
        "agent mode",
        "side by side",
        "compare 2 models of your choice",
        "chat with 1 model at a time",
        "inputs are processed by third-party ai",
        "security verification",
        "protected by recaptcha",
        "recaptcha",
        "thought for ",
        "response provided by",
        "chat completed",
        "stop generating",
        "performing security verification",
    )

    return any(
        marker in lower
        for marker
        in markers
    )


async def arena_stream(
    payload,
    headers,
):

    model = str(
        payload.get(
            "model",
            "",
        )
        or ""
    )

    async with ARENA_LOCK:

        session_id = uuid.uuid4().hex

        activate_session(
            session_id
        )

        upstream_payload = dict(
            payload
        )

        # The backend itself may still buffer Arena output.
        # We don't use that stream. We stream directly from the DOM.
        upstream_payload[
            "stream"
        ] = False

        task = asyncio.create_task(
            call_backend_json(
                upstream_payload,
                headers,
            )
        )

        seen_reasoning = ""

        seen_content = ""

        try:

            # Immediately open the OpenAI SSE stream.
            yield chunk(
                model,
                {
                    "role":
                        "assistant"
                },
            )

            task_done_at = None

            while True:

                drained = False

                while True:

                    try:

                        event = EVENT_QUEUE.get_nowait()

                    except queue.Empty:
                        break


                    if (
                        event.get(
                            "session"
                        )
                        != session_id
                    ):
                        continue


                    drained = True

                    reasoning = (
                        _turntoapi_sanitize_arena_text(
                            event.get(
                                "reasoning",
                                "",
                            )
                            or ""
                        )
                    )

                    content = (
                        _turntoapi_sanitize_arena_text(
                            event.get(
                                "content",
                                "",
                            )
                            or ""
                        )
                    )


                    # Arena normally appends text. Stream only genuine
                    # append deltas; never duplicate rewritten text.

                    if reasoning.startswith(
                        seen_reasoning
                    ):

                        suffix = reasoning[
                            len(
                                seen_reasoning
                            ):
                        ]

                        if suffix:

                            seen_reasoning = reasoning

                            yield chunk(
                                model,
                                {
                                    "reasoning_content":
                                        suffix
                                },
                            )


                    if content.startswith(
                        seen_content
                    ):

                        suffix = content[
                            len(
                                seen_content
                            ):
                        ]

                        if suffix:

                            seen_content = content

                            yield chunk(
                                model,
                                {
                                    "content":
                                        suffix
                                },
                            )


                if task.done():

                    if task_done_at is None:

                        task_done_at = (
                            time.monotonic()
                        )


                    # Small grace period for final DOM mutation.
                    if (
                        not drained
                        and
                        time.monotonic()
                        - task_done_at
                        >= 0.20
                    ):
                        break


                await asyncio.sleep(
                    0.025
                )


            backend_response = await task


            if backend_response.status_code >= 400:

                yield (
                    "data: "
                    + json.dumps(
                        {
                            "error": {
                                "message":
                                    backend_response.text[
                                        :4000
                                    ],

                                "type":
                                    "turntoapi_backend_error",
                            }
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

                yield "data: [DONE]\n\n"

                return


            data, message = message_from_response(
                backend_response
            )


            if message is not None:

                backend_reasoning = (
                    _turntoapi_sanitize_arena_text(
                        message.get(
                            "reasoning_content"
                        )
                        or
                        message.get(
                            "reasoning"
                        )
                        or ""
                    )
                )

                backend_content = (
                    _turntoapi_sanitize_arena_text(
                        message.get(
                            "content"
                        )
                        or ""
                    )
                )


                # Structured backend reasoning is a safe fallback.
                if (
                    not seen_reasoning
                    and backend_reasoning
                ):

                    seen_reasoning = (
                        backend_reasoning
                    )

                    yield chunk(
                        model,
                        {
                            "reasoning_content":
                                backend_reasoning
                        },
                    )


                # Never fall back to a known polluted body scrape.
                if (
                    not seen_content
                    and backend_content
                    and not looks_polluted(
                        backend_content
                    )
                ):

                    seen_content = (
                        backend_content
                    )

                    yield chunk(
                        model,
                        {
                            "content":
                                backend_content
                        },
                    )


            if not seen_content:

                yield (
                    "data: "
                    + json.dumps(
                        {
                            "error": {
                                "message":
                                    (
                                        "Arena finished, but TurnToAPI "
                                        "did not receive a clean final-answer "
                                        "DOM stream. Raw browser-wide text "
                                        "was deliberately not returned."
                                    ),

                                "type":
                                    "turntoapi_dom_stream_missing",
                            }
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

                yield "data: [DONE]\n\n"

                return


            yield chunk(
                model,
                {},
                "stop",
            )

            yield "data: [DONE]\n\n"


        except Exception as exc:

            if not task.done():

                task.cancel()


            yield (
                "data: "
                + json.dumps(
                    {
                        "error": {
                            "message":
                                repr(
                                    exc
                                ),

                            "type":
                                "turntoapi_live_proxy_error",
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )

            yield "data: [DONE]\n\n"


        finally:

            deactivate_session(
                session_id
            )


async def arena_nonstream(
    payload,
    headers,
):

    async with ARENA_LOCK:

        session_id = uuid.uuid4().hex

        activate_session(
            session_id
        )

        try:

            upstream_payload = dict(
                payload
            )

            upstream_payload[
                "stream"
            ] = False

            response = await call_backend_json(
                upstream_payload,
                headers,
            )


            if response.status_code >= 400:

                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=response_headers(
                        response
                    ),
                    media_type=response.headers.get(
                        "content-type"
                    ),
                )


            data, message = message_from_response(
                response
            )

            event = snapshot(
                session_id
            )


            if (
                data is not None
                and message is not None
            ):

                if event:

                    reasoning = str(
                        event.get(
                            "reasoning",
                            "",
                        )
                        or ""
                    )

                    content = str(
                        event.get(
                            "content",
                            "",
                        )
                        or ""
                    )

                    if reasoning:

                        message[
                            "reasoning_content"
                        ] = reasoning

                    if content:

                        message[
                            "content"
                        ] = content


                current_content = str(
                    message.get(
                        "content"
                    )
                    or ""
                )


                if looks_polluted(
                    current_content
                ):

                    return JSONResponse(
                        status_code=502,
                        content={
                            "error": {
                                "message":
                                    (
                                        "Arena response contained browser "
                                        "reasoning/UI text in assistant.content. "
                                        "TurnToAPI refused to expose it."
                                    ),

                                "type":
                                    "turntoapi_polluted_content",
                            }
                        },
                    )


                return JSONResponse(
                    content=data,
                    status_code=response.status_code,
                )


            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers(
                    response
                ),
                media_type=response.headers.get(
                    "content-type"
                ),
            )


        finally:

            deactivate_session(
                session_id
            )


async def generic_proxy(
    request,
    path,
    body,
):

    headers = forward_headers(
        request
    )

    url = backend_url(
        path,
        request,
    )

    stream_requested = False


    if path == "v1/chat/completions":

        try:

            parsed = json.loads(
                body.decode(
                    "utf-8"
                )
            )

            stream_requested = bool(
                parsed.get(
                    "stream"
                )
            )

        except Exception:
            pass


    if stream_requested:

        async def upstream():

            async with httpx.AsyncClient(
                timeout=None
            ) as client:

                async with client.stream(
                    request.method,
                    url,
                    content=body,
                    headers=headers,
                ) as response:

                    async for part in response.aiter_raw():

                        yield part


        return StreamingResponse(
            upstream(),
            media_type="text/event-stream",
        )


    async with httpx.AsyncClient(
        timeout=None
    ) as client:

        response = await client.request(
            request.method,
            url,
            content=body,
            headers=headers,
        )


    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=response_headers(
            response
        ),
        media_type=response.headers.get(
            "content-type"
        ),
    )


@app.get(
    "/__turntoapi/live-status"
)
def live_status():

    with STATE_LOCK:

        active = ACTIVE_SESSION

        count = EVENT_COUNT

        last = dict(
            LAST_EVENT
        )


    return {
        "live_proxy":
            True,

        "arena_browser":
            "playwright-firefox-persistent",

        "firefox_janitor":
            "disabled",

        "observer_transport":
            "playwright-persistent-firefox",

        "observer_interval_ms":
            60,

        "backend":
            BACKEND,

        "observer_port":
            EVENT_PORT,

        "active_session":
            bool(
                active
            ),

        "observer_events":
            count,

        "last_reasoning_chars":
            len(
                str(
                    last.get(
                        "reasoning",
                        "",
                    )
                    or ""
                )
            ),

        "last_content_chars":
            len(
                str(
                    last.get(
                        "content",
                        "",
                    )
                    or ""
                )
            ),

        "security_block":
            bool(
                last.get(
                    "security_block",
                    False,
                )
            ),
    }


@app.api_route(
    "/{path:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],
)
async def proxy(
    request: Request,
    path: str,
):

    body = await request.body()


    if (
        request.method == "POST"
        and path == "v1/chat/completions"
    ):

        try:

            payload = json.loads(
                body.decode(
                    "utf-8"
                )
            )

        except Exception:

            payload = None


        if isinstance(
            payload,
            dict,
        ):

            model = str(
                payload.get(
                    "model",
                    "",
                )
                or ""
            )


            if model.startswith(
                "arena:"
            ):

                headers = forward_headers(
                    request
                )


                if bool(
                    payload.get(
                        "stream"
                    )
                ):

                    return StreamingResponse(
                        arena_stream(
                            payload,
                            headers,
                        ),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control":
                                "no-cache",

                            "X-Accel-Buffering":
                                "no",

                            "Connection":
                                "keep-alive",
                        },
                    )


                return await arena_nonstream(
                    payload,
                    headers,
                )


    return await generic_proxy(
        request,
        path,
        body,
    )


def main():

    global BACKEND

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--host",
        required=True,
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8000,
    )

    parser.add_argument(
        "--backend",
        default="http://127.0.0.1:8001",
    )

    args = parser.parse_args()

    BACKEND = args.backend.rstrip(
        "/"
    )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":

    main()