#!/usr/bin/env python3
"""
TurnToAPI Server: Bridge websites and chatbots into OpenAI-compatible /v1/chat/completions endpoints.


Features:
- OpenAI API compatibility: /v1/chat/completions (streaming SSE & non-streaming JSON) and /v1/models.
- Website Adapter: Converts any website/documentation URL into an interactive conversational endpoint.
  Supports both standalone extractive QA and LLM-augmented synthesis.
- Chatbot Adapter: Translates proprietary or custom chatbot REST/webhook/SSE APIs into the standard OpenAI format.
- Dynamic Routing: Call endpoints on the fly using `model: "website:https://..."` or `model: "chatbot:https://..."`
- Configuration File: Define custom named model endpoints in YAML/JSON.
"""


import os
import re
import sys
import time
import json
import uuid
import asyncio
import logging

from browser_web_adapter import BrowserWebAdapter
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse


import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import bs4
import html2text


try:
    import yaml
except ImportError:
    yaml = None


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TurnToAPI")


def to_json(model_obj: Any) -> str:
    """Compatible with both Pydantic v1 and v2."""
    if hasattr(model_obj, "model_dump_json"):
        return model_obj.model_dump_json()
    return model_obj.json()


def to_dict(model_obj: Any) -> Dict[str, Any]:
    """Compatible with both Pydantic v1 and v2."""
    if hasattr(model_obj, "model_dump"):
        return model_obj.model_dump()
    return model_obj.dict()




# ==============================================================================
# Pydantic Schemas for OpenAI Specification
# ==============================================================================


class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None

    # Separate model reasoning/thinking from user-visible content.
    #
    # Open WebUI can render this separately from normal assistant
    # text instead of dumping reasoning directly into the answer.
    reasoning_content: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None
    top_p: Optional[float] = 1.0


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: UsageInfo


class ChatCompletionChunkDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None

    # OpenAI-compatible reasoning channel used by reasoning-aware
    # clients such as Open WebUI.
    reasoning_content: Optional[str] = None


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]



# ================================================================
# TURNTOAPI_REASONING_SPLITTER_V1
# ================================================================

TURNTOAPI_FINAL_START = "<<<TURNTOAPI_FINAL>>>"
TURNTOAPI_FINAL_END = "<<<END_TURNTOAPI_FINAL>>>"


def _turntoapi_clean_arena_text(value: str) -> str:
    """
    Remove known Arena UI/status strings without touching real prose.
    """

    if not value:
        return ""

    ignored = {
        "",
        "arena code completion request",
        "building",
        "building...",
        "generating",
        "generating...",
        "download",
        "no preview available",
        "preview will appear when agent is done working",
        "inputs are processed by third-party ai and responses may be inaccurate.",
        "security verification",
        "protected by recaptcha",
    }

    result = []

    for raw in value.replace(
        "\r\n",
        "\n"
    ).replace(
        "\r",
        "\n"
    ).split("\n"):

        line = raw.strip()

        if not line:
            continue

        if line.lower() in ignored:
            continue

        result.append(
            line
        )

    return "\n".join(
        result
    ).strip()


def split_reasoning_content(
    raw_content: str,
    model_id: str,
    messages=None,
):
    """
    Split Arena's visible thought/progress transcript from the
    user-visible answer.

    Important:

    We no longer inject output-format instructions into Arena.

    reasoning_content:
        Anything Arena visibly exposed as reasoning/progress that can
        be separated with confidence.

    content:
        Final user-facing answer.

    For ambiguous ordinary responses we preserve text rather than
    guessing destructively.
    """

    raw_content = str(
        raw_content or ""
    )

    if not model_id.startswith(
        "arena:"
    ):
        return (
            raw_content,
            None,
        )

    text = raw_content.replace(
        "\r\n",
        "\n"
    ).replace(
        "\r",
        "\n"
    )

    # ------------------------------------------------------------
    # Remove obsolete TurnToAPI protocol echoes left behind by old
    # conversations/tests.
    # ------------------------------------------------------------

    text = re.sub(
        r"(?ims)"
        r"^TURNTOAPI OUTPUT PROTOCOL V1:\s*"
        r".*?"
        r"Do not omit the markers\.\s*",
        "",
        text,
    )

    # Remove literal marker lines if an old Arena generation echoed
    # them.
    text = text.replace(
        "<<<TURNTOAPI_FINAL>>>",
        ""
    ).replace(
        "<<<END_TURNTOAPI_FINAL>>>",
        ""
    )

    cleaned = _turntoapi_clean_arena_text(
        text
    )

    # ------------------------------------------------------------
    # Find the latest user message.
    # ------------------------------------------------------------

    last_user_text = ""

    if messages:
        try:
            for message in reversed(
                messages
            ):
                if getattr(
                    message,
                    "role",
                    None,
                ) == "user":

                    last_user_text = str(
                        getattr(
                            message,
                            "content",
                            "",
                        )
                    ).strip()

                    break

        except Exception:
            pass

    # ------------------------------------------------------------
    # EXACT-REPLY MODE
    #
    # This gives us a mathematically reliable final boundary.
    #
    # Example:
    #
    # Reply with exactly: STRUCTURED REASONING WORKING
    #
    # Everything before the FINAL occurrence of the requested target
    # is reasoning/progress/UI transcript.
    # ------------------------------------------------------------

    exact_match = re.search(
        r"(?is)"
        r"reply\s+with\s+exactly:\s*"
        r"(.+?)\s*$",
        last_user_text,
    )

    if exact_match:

        exact_target = exact_match.group(
            1
        ).strip()

        if (
            len(exact_target) >= 2
            and exact_target[0]
            == exact_target[-1]
            and exact_target[0] in {
                '"',
                "'",
            }
        ):
            exact_target = exact_target[
                1:-1
            ].strip()

        # Find the final target occurrence. Arena's own reasoning may
        # mention the requested text multiple times.
        target_position = cleaned.rfind(
            exact_target
        )

        if target_position >= 0:

            before = cleaned[
                :target_position
            ].strip()

            # Remove an echoed copy of the original user prompt.
            if last_user_text:
                before = before.replace(
                    last_user_text,
                    ""
                ).strip()

            # Remove Arena UI/progress lines.
            before = _turntoapi_clean_arena_text(
                before
            )

            # Remove repeated exact target mentions occurring inside
            # the reasoning transcript.
            reasoning_lines = []

            for raw in before.splitlines():

                line = raw.strip()

                if not line:
                    continue

                low = line.lower()

                if line == exact_target:
                    continue

                if low in {
                    "show more",
                    "deployed the project",
                    "created index.html",
                    "updated index.html",
                    "created index.html file",
                    "custom output protocol instructions",
                }:
                    continue

                if re.match(
                    r"^https?://",
                    line,
                    re.I,
                ):
                    continue

                reasoning_lines.append(
                    line
                )

            reasoning = "\n".join(
                reasoning_lines
            ).strip()

            # Strip Arena's visual timing headers. Open WebUI already
            # labels the separate channel as reasoning.
            reasoning = re.sub(
                r"(?im)^Thought for[^\n]*\n?",
                "",
                reasoning,
            ).strip()

            return (
                exact_target,
                reasoning or None,
            )

    # ------------------------------------------------------------
    # GENERIC ARENA MODE
    #
    # Do not pretend we know the final-answer boundary if Arena only
    # gives us one flattened text transcript.
    #
    # We still remove obvious UI/status garbage.
    # ------------------------------------------------------------

    cleaned = re.sub(
        r"(?im)^"
        r"(?:"
        r"Building\.{0,3}|"
        r"Generating\.{0,3}|"
        r"Download|"
        r"Show More|"
        r"No preview available|"
        r"Preview will appear when agent is done working"
        r")"
        r"$",
        "",
        cleaned,
    )

    cleaned = re.sub(
        r"\n{3,}",
        "\n\n",
        cleaned,
    ).strip()

    return (
        cleaned,
        None,
    )



class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "turn-to-api"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelCard]


ARENA_MODES = {
    "arena:mode:battle": "Battle mode: anonymous head-to-head model comparison.",
    "arena:mode:direct": "Direct mode: one selected model at a time.",
    "arena:mode:side-by-side": "Side-by-Side mode: compare two selected models.",
    "arena:mode:agent": "Agent mode: multi-step tool-using tasks.",
    "arena:mode:auto": "Auto-Mode: route prompts by modality.",
    "arena:mode:max": "Max Smart-Router: latency-aware capable model routing.",
}


ARENA_MODELS = [
    "gemini-4-argon-high",
    "gpt-6.1-sol",
    "sonnet-5.5",
    "claude-fable-5",
    "claude-opus-5-max",
    "gpt-6-sol-max",
    "gpt-6-luna-max",
    "grok-4.7-xhigh",
    "deepseek-v4.1-flash-max",
    "gpt-5.6-sol-xhigh",
    "grok-4.5",
    "qwen3.8-max",
    "qwen3.8-27b",
    "kimi-k3-max",
    "gemini-3.6-flash-high",
    "gemini-3.1-pro-grounding",
]


CHATGPT_MODES = {
    "chatgpt:mode:web": "ChatGPT web chat handoff mode for https://chatgpt.com/.",
}


# ==============================================================================
# In-Memory Cache for Web Pages
# ==============================================================================


class WebCache:
    def __init__(self, ttl_seconds: int = 3600):
        self.ttl = ttl_seconds
        self.cache: Dict[str, Tuple[float, str, str]] = {}


    def get(self, url: str) -> Optional[Tuple[str, str]]:
        if url in self.cache:
            ts, title, content = self.cache[url]
            if time.time() - ts < self.ttl:
                return title, content
            del self.cache[url]
        return None


    def set(self, url: str, title: str, content: str):
        self.cache[url] = (time.time(), title, content)


web_cache = WebCache()


def configured_proxy() -> Optional[str]:
    """Return an explicit proxy URL for outbound requests, if configured."""
    for key in ("TURNTOAPI_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = os.environ.get(key) or os.environ.get(key.lower())
        if value:
            return value
    return None


def new_http_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an outbound HTTP client that honors proxy environment settings."""
    proxy = configured_proxy()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    return httpx.AsyncClient(trust_env=True, **kwargs)


# ==============================================================================
# Helper Functions: Scraping & Content Processing
# ==============================================================================


def clean_html_to_markdown(html_content: str, base_url: str = "") -> Tuple[str, str]:
    soup = bs4.BeautifulSoup(html_content, "lxml" if "lxml" in sys.modules else "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else "Web Page"


    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "aside"]):
        tag.decompose()


    main_el = soup.find("main") or soup.find("article") or soup.find("div", {"role": "main"}) or soup.body
    target_soup = main_el if main_el else soup


    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_tables = False
    h.body_width = 0
    markdown = h.handle(str(target_soup)).strip()
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return title, markdown


def extract_relevant_sections(query: str, markdown_content: str, max_chunks: int = 4) -> List[str]:
    paragraphs = [p.strip() for p in markdown_content.split("\n\n") if len(p.strip()) > 30]
    if not paragraphs:
        return [markdown_content[:2000]]


    query_words = set(re.findall(r"\w+", query.lower()))
    if not query_words:
        return paragraphs[:max_chunks]


    scored = []
    for p in paragraphs:
        p_words = set(re.findall(r"\w+", p.lower()))
        overlap = len(query_words.intersection(p_words))
        if overlap > 0:
            scored.append((overlap, p))


    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        return [p for _, p in scored[:max_chunks]]
    return paragraphs[:max_chunks]


# ==============================================================================
# Adapters
# ==============================================================================


class BaseAdapter:
    async def chat(
        self,
        messages: List[ChatMessage],
        stream: bool,
        options: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        raise NotImplementedError


class WebsiteAdapter(BaseAdapter):
    def __init__(self, url: str, name: str = "", upstream_llm: Optional[Dict[str, Any]] = None):
        self.url = url
        self.name = name or url
        self.upstream_llm = upstream_llm


    async def fetch_website_content(self) -> Tuple[str, str]:
        cached = web_cache.get(self.url)
        if cached:
            return cached


        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 TurnToAPI/1.0"
        }
        async with new_http_client(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(self.url, headers=headers)
            resp.raise_for_status()
            html = resp.text


        title, markdown = clean_html_to_markdown(html, self.url)
        web_cache.set(self.url, title, markdown)
        return title, markdown


    async def chat(
        self,
        messages: List[ChatMessage],
        stream: bool,
        options: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        user_message = next((m.content for m in reversed(messages) if m.role == "user"), "")
        try:
            title, markdown = await self.fetch_website_content()
        except Exception as e:
            err_msg = f"Error fetching website {self.url}: {str(e)}"
            logger.error(err_msg)
            yield err_msg
            return


        upstream_api_key = os.environ.get("OPENAI_API_KEY") or (self.upstream_llm.get("api_key") if self.upstream_llm else None)
        upstream_url = os.environ.get("UPSTREAM_LLM_URL") or (self.upstream_llm.get("url") if self.upstream_llm else None)


        if upstream_api_key or upstream_url:
            async for token in self._generate_with_upstream_llm(messages, title, markdown, upstream_api_key, upstream_url):
                yield token
            return


        relevant_chunks = extract_relevant_sections(user_message, markdown, max_chunks=3)
        context = "\n\n---\n\n".join(relevant_chunks)


        header = f"### Information grounded from [{title}]({self.url})\n\n"
        if not user_message:
            body = f"I am connected to **{title}** ({self.url}). Ask me anything about this site!\n\n**Overview Summary:**\n" + markdown[:800] + "..."
        else:
            body = (
                f"Based on the content retrieved from **[{title}]({self.url})**:\n\n"
                f"{context}\n\n"
                f"---\n*Source: [{self.url}]({self.url})*"
            )


        full_response = header + body


        if stream:
            words = re.split(r"(\s+)", full_response)
            for word in words:
                if word:
                    yield word
                    await asyncio.sleep(0.01)
        else:
            yield full_response


    async def _generate_with_upstream_llm(
        self,
        messages: List[ChatMessage],
        title: str,
        markdown: str,
        api_key: Optional[str],
        base_url: Optional[str]
    ) -> AsyncGenerator[str, None]:
        endpoint = (base_url.rstrip("/") + "/chat/completions") if base_url else "https://api.openai.com/v1/chat/completions"
        system_prompt = (
            f"You are a helpful AI assistant representing the website '{title}' ({self.url}).\n"
            f"Answer user questions accurately and thoroughly based primarily on the website's content below.\n\n"
            f"--- WEBSITE CONTENT START ---\n{markdown[:25000]}\n--- WEBSITE CONTENT END ---"
        )


        upstream_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            upstream_messages.append({"role": m.role, "content": m.content})


        headers = {"Authorization": f"Bearer {api_key}" if api_key else ""}
        payload = {
            "model": self.upstream_llm.get("model", "gpt-4o-mini") if self.upstream_llm else "gpt-4o-mini",
            "messages": upstream_messages,
            "stream": True
        }


        async with new_http_client(timeout=60.0) as client:
            async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    yield f"Upstream LLM error: {resp.status_code} {await resp.aread()}"
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            data = json.loads(line[6:])
                            delta = data["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except Exception:
                            continue




class ChatbotAdapter(BaseAdapter):
    def __init__(
        self,
        url: str,
        method: str = "POST",
        headers: Optional[Dict[str, str]] = None,
        request_template: Optional[Dict[str, Any]] = None,
        response_path: Optional[str] = None,
        session_id: Optional[str] = None
    ):
        self.url = url
        self.method = method.upper()
        self.headers = headers or {"Content-Type": "application/json"}
        self.request_template = request_template or {"message": "{last_message}", "query": "{last_message}", "prompt": "{last_message}", "session_id": "{session_id}"}
        self.response_path = response_path
        self.session_id = session_id or str(uuid.uuid4())


    def _extract_from_json(self, data: Any, path: Optional[str]) -> str:
        if path:
            current = data
            for part in path.split("."):
                if isinstance(current, dict):
                    current = current.get(part)
                elif isinstance(current, list) and part.isdigit():
                    current = current[int(part)]
                else:
                    return str(data)
            return str(current) if current is not None else ""


        if isinstance(data, dict):
            candidates = ["reply", "response", "message", "answer", "text", "content", "output"]
            for candidate in candidates:
                if candidate in data and isinstance(data[candidate], str):
                    return data[candidate]
                if candidate in data and isinstance(data[candidate], dict) and "content" in data[candidate]:
                    return data[candidate]["content"]


            # Check nested wrappers: data, result, payload
            for wrapper in ["data", "result", "payload"]:
                if wrapper in data and isinstance(data[wrapper], dict):
                    for candidate in candidates:
                        if candidate in data[wrapper] and isinstance(data[wrapper][candidate], str):
                            return data[wrapper][candidate]


            if "choices" in data and isinstance(data["choices"], list) and len(data["choices"]) > 0:
                choice = data["choices"][0]
                if isinstance(choice, dict):
                    if "message" in choice and isinstance(choice["message"], dict):
                        return choice["message"].get("content", "")
                    if "text" in choice:
                        return choice["text"]
            return json.dumps(data)
        return str(data)


    def _build_payload(self, messages: List[ChatMessage]) -> Dict[str, Any]:
        last_message = next((m.content for m in reversed(messages) if m.role == "user"), "")
        full_history = "\n".join([f"{m.role}: {m.content}" for m in messages])


        def replace_vars(item: Any) -> Any:
            if isinstance(item, str):
                return item.replace("{last_message}", last_message) \
                           .replace("{prompt}", last_message) \
                           .replace("{session_id}", self.session_id) \
                           .replace("{conversation_id}", self.session_id) \
                           .replace("{history}", full_history)
            elif isinstance(item, dict):
                return {k: replace_vars(v) for k, v in item.items()}
            elif isinstance(item, list):
                return [replace_vars(v) for v in item]
            return item


        payload = replace_vars(self.request_template)
        if "{messages}" in str(self.request_template):
            payload["messages"] = [to_dict(m) for m in messages]
        return payload


    async def chat(
        self,
        messages: List[ChatMessage],
        stream: bool,
        options: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        payload = self._build_payload(messages)
        headers = dict(self.headers)


        async with new_http_client(timeout=45.0) as client:
            try:
                if self.method == "GET":
                    resp = await client.get(self.url, params=payload, headers=headers)
                else:
                    resp = await client.post(self.url, json=payload, headers=headers)
                resp.raise_for_status()
            except Exception as e:
                err_text = f"Failed to communicate with chatbot backend at {self.url}: {str(e)}"
                logger.error(err_text)
                yield err_text
                return


            content_type = resp.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            token = self._extract_from_json(data, self.response_path)
                            if token:
                                yield token
                        except Exception:
                            yield data_str
                return


            try:
                data = resp.json()
                text_response = self._extract_from_json(data, self.response_path)
            except Exception:
                text_response = resp.text


            if stream:
                words = re.split(r"(\s+)", text_response)
                for word in words:
                    if word:
                        yield word
                        await asyncio.sleep(0.01)
            else:
                yield text_response




class ArenaAdapter(BaseAdapter):
    def __init__(self, model_id: str):
        self.model_id = model_id


    async def chat(
        self,
        messages: List[ChatMessage],
        stream: bool,
        options: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        user_message = next((m.content for m in reversed(messages) if m.role == "user"), "")
        response = self._response(user_message)
        if stream:
            words = re.split(r"(\s+)", response)
            for word in words:
                if word:
                    yield word
                    await asyncio.sleep(0.01)
        else:
            yield response


    def _response(self, user_message: str) -> str:
        if user_message.strip().lower() in {"greeting", "hello", "hi"}:
            return f"Hello from TurnToAPI Arena ({self.model_id}). Select arena:mode:* entries for modes or arena:direct:<model> for specific Arena models."
        if self.model_id in ARENA_MODES:
            return f"{self.model_id}\n\n{ARENA_MODES[self.model_id]}\n\nAvailable direct models:\n- " + "\n- ".join(ARENA_MODELS)
        if self.model_id.startswith("arena:direct:"):
            model_name = self.model_id.split("arena:direct:", 1)[1]
            return f"Direct mode selected for Arena model: {model_name}."
        if self.model_id.startswith("arena:side-by-side:"):
            return f"Side-by-Side mode selected: {self.model_id}. Use arena:side-by-side:<model-a>:<model-b> with Arena model IDs."
        return "Arena adapter ready. Use arena:mode:* or arena:direct:<model>."


class ChatGPTWebAdapter(BaseAdapter):
    async def chat(
        self,
        messages: List[ChatMessage],
        stream: bool,
        options: Dict[str, Any]
    ) -> AsyncGenerator[str, None]:
        user_message = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if user_message.strip().lower() in {"greeting", "hello", "hi"}:
            response = "Hello from TurnToAPI ChatGPT Web mode. This mode is connected as a selectable Open WebUI model; use it as a handoff to https://chatgpt.com/."
        else:
            response = (
                "ChatGPT web mode is available in the model selector, but chatgpt.com is a browser UI, "
                "not an OpenAI-compatible backend. For automated chats, add an official OpenAI API model; "
                "for web chat, open https://chatgpt.com/ and continue there."
            )
        if stream:
            for word in re.split(r"(\s+)", response):
                if word:
                    yield word
                    await asyncio.sleep(0.01)
        else:
            yield response


# ==============================================================================
# Adapter Registry & Dynamic Router
# ==============================================================================


# ================================================================
# TURNTOAPI_CLEAN_DISCOVERY_V5_BEGIN
# Curated Arena model catalogue exposed through /v1/models.
#
# arena:direct:<model> remains a hidden backwards-compatible alias
# for arena:text:<model>.
# ================================================================

ARENA_TEXT_MODELS = [
    "gemini-4-argon-high",
    "claude-opus-4-6-high",
    "claude-fable-5-high",
    "claude-opus-4-7-high",
    "claude-opus-4-6",
    "claude-opus-5.5-high",
    "gpt-6.1-sol-max",
    "gpt-5.6-sol-xhigh",
    "gemma-4-31b",
]

ARENA_CODE_MODELS = [
    "claude-opus-5.5-max",
    "gpt-6-astra-max",
    "claude-sonnet-5.5-xhigh",
    "gpt-6.1-sol-max",
    "claude-fable-5.1-max",
    "claude-sonnet-5.5-high",
    "claude-opus-5-max",
    "gpt-6-sol-max",
    "gemma-4-31b",
]

ARENA_DISCOVERY_MODES = [
    "battle",
    "auto",
    "agent",
]

# TURNTOAPI_CLEAN_DISCOVERY_V5_END



class AdapterRegistry:
    def __init__(self, config_path: Optional[str] = None):
        self.endpoints: Dict[str, Dict[str, Any]] = {}
        if config_path and os.path.exists(config_path):
            self.load_config(config_path)


    def load_config(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            if config_path.endswith((".yaml", ".yml")) and yaml:
                data = yaml.safe_load(f) or {}
            else:
                data = json.load(f)
            self.endpoints = data.get("endpoints", {})
            logger.info(f"Loaded {len(self.endpoints)} configured endpoints from {config_path}")


    def list_models(self) -> List[ModelCard]:
        """
        Return only useful concrete models to Open WebUI.

        Legacy arena:direct:* IDs are intentionally NOT advertised,
        although resolve_adapter() still accepts them.
        """

        cards = []
        now = int(time.time())

        seen = set()

        def add(model_id: str, owner: str):
            if model_id in seen:
                return

            seen.add(model_id)

            cards.append(
                ModelCard(
                    id=model_id,
                    created=now,
                    owned_by=owner
                )
            )

        # Preserve user's ordinary configured endpoints, but never
        # advertise stale Arena / ChatGPT placeholder config entries.
        for model_id, conf in self.endpoints.items():

            if (
                model_id.startswith("arena:")
                or model_id == "chatgpt:mode:web"
            ):
                continue

            add(
                model_id,
                conf.get(
                    "type",
                    "custom"
                )
            )

        # ----------------------------------------------------------
        # ChatGPT web session
        # ----------------------------------------------------------

        add(
            "chatgpt:mode:web",
            "chatgpt-web"
        )

        # ----------------------------------------------------------
        # Arena modes
        # ----------------------------------------------------------

        for mode in ARENA_DISCOVERY_MODES:
            add(
                f"arena:mode:{mode}",
                "arena-mode"
            )

        # ----------------------------------------------------------
        # Arena Text Direct
        # ----------------------------------------------------------

        for model in ARENA_TEXT_MODELS:
            add(
                f"arena:text:{model}",
                "arena-text"
            )

        # ----------------------------------------------------------
        # Arena Code Direct
        # ----------------------------------------------------------

        for model in ARENA_CODE_MODELS:
            add(
                f"arena:code:{model}",
                "arena-code"
            )

        # Keep TurnToAPI generic dynamic routes.
        add(
            "website:<url>",
            "dynamic-website"
        )

        add(
            "chatbot:<url>",
            "dynamic-chatbot"
        )

        # TURNTOAPI_TRIGGERED_ARENA_REGISTRY_V1_BEGIN
        #
        # Read Arena's live registry on every /v1/models request.
        #
        # The file is refreshed ONLY when a previously verified model
        # unexpectedly redirects to Arena's Max router.
        #
        try:
            arena_registry_path = os.path.join(
                os.path.dirname(
                    os.path.abspath(__file__)
                ),
                "arena_models_live.json",
            )

            with open(
                arena_registry_path,
                "r",
                encoding="utf-8-sig",
            ) as arena_registry_file:
                arena_registry = json.load(
                    arena_registry_file
                )

            cards = [
                card
                for card in cards
                if not (
                    card.id.startswith(
                        "arena:text:"
                    )
                    or card.id.startswith(
                        "arena:code:"
                    )
                )
            ]

            seen = {
                card.id
                for card in cards
            }

            for arena_mode in (
                "text",
                "code",
            ):

                for arena_item in arena_registry.get(
                    arena_mode,
                    [],
                ):

                    if isinstance(
                        arena_item,
                        dict,
                    ):
                        arena_model_id = str(
                            arena_item.get(
                                "id",
                                "",
                            )
                        ).strip()

                    else:
                        arena_model_id = str(
                            arena_item
                        ).strip()

                    if (
                        not arena_model_id
                        or arena_model_id.lower()
                        == "max"
                    ):
                        continue

                    add(
                        (
                            "arena:"
                            + arena_mode
                            + ":"
                            + arena_model_id
                        ),
                        "arena",
                    )

        except Exception as exc:
            logging.warning(
                "Could not load triggered Arena registry: %s",
                exc,
            )

        # TURNTOAPI_TRIGGERED_ARENA_REGISTRY_V1_END

        return cards


    def resolve_adapter(self, model_id: str) -> BaseAdapter:
        # TURNTOAPI_TRIGGERED_ARENA_ROUTING_V1_BEGIN

        if model_id.startswith(
            "arena:text:"
        ):

            arena_model = model_id[
                len("arena:text:"):
            ].strip()

            if not arena_model:
                raise ValueError(
                    "Arena text model ID is empty."
                )

            return BrowserWebAdapter(
                provider="arena",
                mode="text",
                model=arena_model,
            )

        if model_id.startswith(
            "arena:code:"
        ):

            arena_model = model_id[
                len("arena:code:"):
            ].strip()

            if not arena_model:
                raise ValueError(
                    "Arena code model ID is empty."
                )

            return BrowserWebAdapter(
                provider="arena",
                mode="code",
                model=arena_model,
            )

        # TURNTOAPI_TRIGGERED_ARENA_ROUTING_V1_END

        # TURNTOAPI_CLEAN_ROUTING_V5_BEGIN

        # ----------------------------------------------------------
        # ChatGPT Web
        # ----------------------------------------------------------

        if model_id == "chatgpt:mode:web":
            return BrowserWebAdapter(
                provider="chatgpt",
                mode="web"
            )

        # ----------------------------------------------------------
        # Arena Code Direct
        #
        # URL:
        # https://arena.ai/code/direct?model_a=<model>
        # ----------------------------------------------------------

        if model_id.startswith("arena:code:"):
            arena_model = model_id.split(":", 2)[2]

            return BrowserWebAdapter(
                provider="arena",
                mode="code",
                model=arena_model
            )

        # ----------------------------------------------------------
        # Arena Text Direct
        #
        # URL:
        # https://arena.ai/text/direct?model_a=<model>
        # ----------------------------------------------------------

        if model_id.startswith("arena:text:"):
            arena_model = model_id.split(":", 2)[2]

            return BrowserWebAdapter(
                provider="arena",
                mode="text",
                model=arena_model
            )

        # ----------------------------------------------------------
        # Legacy alias - intentionally hidden from /v1/models.
        #
        # Old conversations using:
        #
        # arena:direct:<model>
        #
        # continue to work as Text Direct.
        # ----------------------------------------------------------

        if model_id.startswith("arena:direct:"):
            arena_model = model_id.split(":", 2)[2]

            return BrowserWebAdapter(
                provider="arena",
                mode="text",
                model=arena_model
            )

        # ----------------------------------------------------------
        # Arena modes
        # ----------------------------------------------------------

        if model_id.startswith("arena:mode:"):
            arena_mode = model_id.split(":", 2)[2]

            supported_modes = {
                "battle",
                "auto",
                "agent",
            }

            if arena_mode not in supported_modes:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Arena mode '{arena_mode}' is not "
                        "currently exposed. Supported modes: "
                        f"{sorted(supported_modes)}"
                    )
                )

            return BrowserWebAdapter(
                provider="arena",
                mode=arena_mode
            )

        # TURNTOAPI_CLEAN_ROUTING_V5_END


        # TURNTOAPI_REAL_FIREFOX_ROUTING_V3

        if model_id == "chatgpt:mode:web":
            return BrowserWebAdapter(
                provider="chatgpt",
                mode="web"
            )

        # TURNTOAPI_ARENA_MODALITY_URL_ROUTING

        if model_id.startswith("arena:code:"):
            arena_model = model_id.split(":", 2)[2]
            return BrowserWebAdapter(
                provider="arena",
                mode="code",
                model=arena_model
            )

        if model_id.startswith("arena:text:"):
            arena_model = model_id.split(":", 2)[2]
            return BrowserWebAdapter(
                provider="arena",
                mode="text",
                model=arena_model
            )


        if model_id.startswith("arena:direct:"):
            arena_model = model_id.split(":", 2)[2]
            return BrowserWebAdapter(
                provider="arena",
                mode="direct",
                model=arena_model
            )

        if model_id.startswith("arena:mode:"):
            arena_mode = model_id.split(":", 2)[2]
            return BrowserWebAdapter(
                provider="arena",
                mode=arena_mode
            )

        # TURNTOAPI_REAL_BROWSER_ROUTING_V2
        #
        # These routes intentionally execute before older Arena/ChatGPT
        # placeholder adapters.

        if model_id == "chatgpt:mode:web":
            return BrowserWebAdapter(
                provider="chatgpt",
                mode="web"
            )

        if model_id.startswith("arena:direct:"):
            arena_model = model_id.split(":", 2)[2]
            return BrowserWebAdapter(
                provider="arena",
                mode="direct",
                model=arena_model
            )

        if model_id.startswith("arena:mode:"):
            arena_mode = model_id.split(":", 2)[2]
            return BrowserWebAdapter(
                provider="arena",
                mode=arena_mode
            )



        # TURNTOPAPI_BROWSER_ROUTING


        # Browser-backed ChatGPT web


        if model_id == "chatgpt:mode:web":


            return BrowserWebAdapter(


                provider="chatgpt",


                mode="web",


            )



        # Browser-backed Arena direct model


        if model_id.startswith("arena:direct:"):


            arena_model = model_id.split(":", 2)[2]


            return BrowserWebAdapter(


                provider="arena",


                mode="direct",


                model=arena_model,


            )



        # Browser-backed Arena modes


        if model_id.startswith("arena:mode:"):


            arena_mode = model_id.split(":", 2)[2]


            return BrowserWebAdapter(


                provider="arena",


                mode=arena_mode,


            )
        if model_id in self.endpoints:
            conf = self.endpoints[model_id]
            adapter_type = conf.get("type", "").lower()
            if adapter_type == "website":
                return WebsiteAdapter(
                    url=conf["url"],
                    name=conf.get("title", model_id),
                    upstream_llm=conf.get("upstream_llm")
                )
            elif adapter_type == "chatbot":
                return ChatbotAdapter(
                    url=conf["url"],
                    method=conf.get("method", "POST"),
                    headers=conf.get("headers"),
                    request_template=conf.get("request_template"),
                    response_path=conf.get("response_path"),
                    session_id=conf.get("session_id")
                )
            else:
                raise HTTPException(status_code=400, detail=f"Unknown adapter type '{adapter_type}' for model '{model_id}'")


        if model_id in ARENA_MODES or model_id.startswith(("arena:direct:", "arena:side-by-side:")):
            return ArenaAdapter(model_id)

        if model_id in CHATGPT_MODES:
            return ChatGPTWebAdapter()


        if model_id.startswith(("website:", "web:")):
            url = model_id.split(":", 1)[1]
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            return WebsiteAdapter(url=url, name=model_id)


        if model_id.startswith(("chatbot:", "bot:")):
            url = model_id.split(":", 1)[1]
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            return ChatbotAdapter(url=url)


        if model_id.startswith(("http://", "https://")):
            return WebsiteAdapter(url=model_id, name=model_id)


        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_id}' not found. Configured models: {list(self.endpoints.keys())}. "
                   f"Or use dynamic routing: 'website:<url>' or 'chatbot:<url>'."
        )




# ==============================================================================
# FastAPI Application
# ==============================================================================


app = FastAPI(
    title="TurnToAPI Server",
    description="Turn websites and chatbots into OpenAI-compatible /v1/chat endpoints",
    version="1.0.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


registry = AdapterRegistry()


@app.on_event("startup")
async def startup_event():
    config_file = os.environ.get("CONFIG_FILE", "config.yaml")
    if os.path.exists(config_file):
        registry.load_config(config_file)
    elif os.path.exists("config.json"):
        registry.load_config("config.json")


@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "online",
        "service": "TurnToAPI Server",
        "endpoints": {
            "chat_completions": "/v1/chat/completions",
            "models": "/v1/models"
        }
    }


@app.get("/v1/models", response_model=ModelListResponse)
async def list_models():
    return ModelListResponse(data=registry.list_models())


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    adapter = registry.resolve_adapter(req.model)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_time = int(time.time())


    token_generator = adapter.chat(
        messages=req.messages,
        stream=bool(req.stream),
        options={"temperature": req.temperature, "max_tokens": req.max_tokens}
    )


    if req.stream:
        async def event_stream() -> AsyncGenerator[str, None]:

            first_chunk = ChatCompletionChunk(
                id=completion_id,
                created=created_time,
                model=req.model,
                choices=[
                    ChatCompletionChunkChoice(
                        index=0,
                        delta=ChatCompletionChunkDelta(
                            role="assistant"
                        ),
                        finish_reason=None
                    )
                ]
            )

            yield f"data: {to_json(first_chunk)}\n\n"

            # ------------------------------------------------------
            # Arena browser responses are completed in the browser
            # before being returned. Buffer them so reasoning and
            # final content can be separated cleanly.
            # ------------------------------------------------------

            if req.model.startswith("arena:"):

                buffered = []

                async for token in token_generator:
                    buffered.append(
                        token
                    )

                raw_content = "".join(
                    buffered
                )

                visible_content, reasoning_content = (
                    split_reasoning_content(
                        raw_content,
                        req.model,
                        req.messages,
                    )
                )

                if reasoning_content:

                    reasoning_chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created_time,
                        model=req.model,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(
                                    reasoning_content=reasoning_content
                                ),
                                finish_reason=None
                            )
                        ]
                    )

                    yield (
                        f"data: {to_json(reasoning_chunk)}\n\n"
                    )

                if visible_content:

                    content_chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created_time,
                        model=req.model,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(
                                    content=visible_content
                                ),
                                finish_reason=None
                            )
                        ]
                    )

                    yield (
                        f"data: {to_json(content_chunk)}\n\n"
                    )

            else:

                async for token in token_generator:

                    chunk = ChatCompletionChunk(
                        id=completion_id,
                        created=created_time,
                        model=req.model,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(
                                    content=token
                                ),
                                finish_reason=None
                            )
                        ]
                    )

                    yield f"data: {to_json(chunk)}\n\n"

            final_chunk = ChatCompletionChunk(
                id=completion_id,
                created=created_time,
                model=req.model,
                choices=[
                    ChatCompletionChunkChoice(
                        index=0,
                        delta=ChatCompletionChunkDelta(),
                        finish_reason="stop"
                    )
                ]
            )

            yield f"data: {to_json(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream"
        )


    full_text_parts = []
    async for token in token_generator:
        full_text_parts.append(token)
    full_content = "".join(full_text_parts)


    prompt_words = sum(len(m.content.split()) for m in req.messages)
    completion_words = len(full_content.split())


    # TURNTOAPI_REASONING_SPLIT_NONSTREAM
    visible_content, reasoning_content = split_reasoning_content(
        full_content,
        req.model,
        req.messages,
    )

    response = ChatCompletionResponse(
        id=completion_id,
        created=created_time,
        model=req.model,
        choices=[
            ChatCompletionResponseChoice(
                index=0,
                message=ChatMessage(
                    role="assistant",
                    content=visible_content,
                    reasoning_content=reasoning_content,
                ),
                finish_reason="stop"
            )
        ],
        usage=UsageInfo(
            prompt_tokens=prompt_words,
            completion_tokens=completion_words,
            total_tokens=prompt_words + completion_words
        )
    )
    return response


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Turn websites and chatbots into v1/chat API endpoints")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml or config.json")
    args = parser.parse_args()


    os.environ["CONFIG_FILE"] = args.config
    print(f"Starting TurnToAPI Server on http://{args.host}:{args.port}")
    print(f"OpenAI-compatible endpoint: http://{args.host}:{args.port}/v1/chat/completions")
    print(f"Models list endpoint:     http://{args.host}:{args.port}/v1/models")
    uvicorn.run(app, host=args.host, port=args.port)
