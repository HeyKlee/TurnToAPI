from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, parse_qs, urlparse

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from arena_model_registry import get_model, get_route, mark_unverified, mark_verified


ROOT = Path(__file__).resolve().parent


SECURITY_MARKERS = (
    "security verification",
    "please complete this quick security check to continue",
    "protected by recaptcha",
    "recaptcha",
    "verify you are human",
    "checking your browser",
    "just a moment",
)

ARENA_UI_LINES = {
    "Battle Mode",
    "Battle",
    "Battle 2 anonymous models",
    "Agent Mode",
    "Built for complex tasks",
    "Side by Side",
    "Compare 2 models of your choice",
    "Direct",
    "Chat with 1 model at a time",
    "Inputs are processed by third-party AI and responses may be inaccurate.",
    "Stop generating",
    "Generating...",
    "Building...",
    "Response provided by",
}


@dataclass
class BrowserResult:
    content: str
    reasoning_content: str = ""
    final_url: str = ""


def _clean_text(value: str | None, *, reasoning: bool = False) -> str:
    value = str(value or "").replace("\r", "")
    if not value:
        return ""

    lower = value.lower()
    if any(marker in lower for marker in SECURITY_MARKERS):
        return ""

    kept: list[str] = []
    for raw in value.split("\n"):
        line = raw.strip()
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if line in ARENA_UI_LINES:
            continue
        if line.startswith("Preview will appear"):
            continue
        if reasoning and re.match(r"^Thought for \d+(?:\.\d+)? seconds?$", line, re.I):
            continue
        kept.append(raw.strip())

    text = "\n".join(kept).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""

    if len(messages) == 1 and messages[0].get("role") == "user":
        return str(messages[0].get("content") or "")

    blocks: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        content = message.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(str(item))
            content = "\n".join(parts)
        blocks.append(f"{role}:\n{content}")

    blocks.append("ASSISTANT:")
    return "\n\n".join(blocks)


class _BrowserWorker:
    """
    Owns Playwright and all browser objects on exactly one thread.
    Requests are serialized through a queue. This avoids the Selenium/
    Playwright cross-thread problems that caused unstable Firefox sessions
    in the prototype.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.commands: queue.Queue[Any] = queue.Queue()
        self.ready = threading.Event()
        self.start_error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="TurnToAPI-BrowserWorker",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(120):
            raise RuntimeError("Timed out starting TurnToAPI browser worker.")
        if self.start_error:
            raise RuntimeError(
                "Could not start TurnToAPI browser worker: "
                + repr(self.start_error)
            )

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        response: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.commands.put((name, args, kwargs, response))
        try:
            ok, value = response.get(timeout=1200)
        except queue.Empty:
            raise TimeoutError("Timed out waiting for browser worker.")
        if ok:
            return value
        raise value

    def _run(self) -> None:
        try:
            self._launch()
        except BaseException as exc:
            self.start_error = exc
            self.ready.set()
            return

        self.ready.set()

        while True:
            item = self.commands.get()
            if item is None:
                self._shutdown()
                return
            name, args, kwargs, response = item
            try:
                value = getattr(self, f"_op_{name}")(*args, **kwargs)
                response.put((True, value))
            except BaseException as exc:
                response.put((False, exc))

    def _launch(self) -> None:
        self.pw = sync_playwright().start()
        self.contexts: dict[str, Any] = {}
        self.pages: dict[str, Any] = {}

    def _shutdown(self) -> None:
        for context in list(self.contexts.values()):
            try:
                context.close()
            except Exception:
                pass
        try:
            self.pw.stop()
        except Exception:
            pass

    def _settings(self, provider: str) -> dict[str, Any]:
        settings = dict(self.config.get(provider, {}) or {})
        return settings

    def _page(self, provider: str):
        if provider in self.pages:
            page = self.pages[provider]
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass

        settings = self._settings(provider)
        profile_raw = str(
            settings.get("profile_dir")
            or f"playwright_profiles/{provider}"
        )
        profile = Path(profile_raw)
        if not profile.is_absolute():
            profile = ROOT / profile
        profile.mkdir(parents=True, exist_ok=True)

        headed = bool(settings.get("headed", True))
        context = self.pw.firefox.launch_persistent_context(
            user_data_dir=str(profile),
            headless=not headed,
            viewport={"width": 1440, "height": 1000},
            firefox_user_prefs={
                "dom.webnotifications.enabled": False,
                "browser.shell.checkDefaultBrowser": False,
            },
        )
        nav_ms = int(
            float(settings.get("navigation_timeout_seconds", 90)) * 1000
        )
        context.set_default_timeout(15000)
        context.set_default_navigation_timeout(nav_ms)

        page = context.pages[0] if context.pages else context.new_page()
        self.contexts[provider] = context
        self.pages[provider] = page
        return page

    def _body_text(self, page: Any) -> str:
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    def _is_security(self, page: Any) -> bool:
        lower = self._body_text(page).lower()
        return any(marker in lower for marker in SECURITY_MARKERS)

    def _wait_for_human_verification(self, page: Any, provider: str) -> None:
        if not self._is_security(page):
            return

        settings = self._settings(provider)
        timeout = float(settings.get("verification_timeout_seconds", 900))
        deadline = time.monotonic() + timeout

        print(
            f"[TurnToAPI] {provider} security verification detected. "
            "Complete it manually in the dedicated browser."
        )

        while time.monotonic() < deadline:
            time.sleep(1.0)
            if not self._is_security(page):
                print(f"[TurnToAPI] {provider} verification cleared.")
                return

        raise RuntimeError(
            f"{provider} security verification did not clear within "
            f"{int(timeout)} seconds."
        )

    def _goto(self, page: Any, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            # A browser UI can still be usable after a navigation timeout.
            pass

    def _find_composer(self, page: Any):
        selectors = (
            'textarea[placeholder*="Message" i]',
            'textarea[placeholder*="Ask" i]',
            "textarea",
            '[contenteditable="true"][role="textbox"]',
            '[contenteditable="true"]',
        )
        for selector in selectors:
            locator = page.locator(selector)
            count = locator.count()
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        raise RuntimeError("Could not find the web chat composer.")

    def _arena_snapshot(self, page: Any) -> dict[str, Any]:
        return page.evaluate(
            r"""
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = getComputedStyle(el);
                if (style.display === "none" || style.visibility === "hidden") {
                  return false;
                }
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };

              const cards = [...document.querySelectorAll(
                "div.flex.flex-col.gap-3"
              )].filter(visible);

              const card = cards.length ? cards[cards.length - 1] : null;
              if (!card) {
                return {
                  cardCount: cards.length,
                  reasoning: "",
                  content: "",
                  generating: false
                };
              }

              const thought = card.querySelector("div.not-prose[data-state]");
              let reasoning = "";
              if (thought) {
                reasoning = String(
                  thought.innerText || thought.textContent || ""
                );
              }

              const clone = card.cloneNode(true);
              clone.querySelectorAll(
                "div.not-prose[data-state], button, [role=button], " +
                "[data-testid*=action], [data-testid*=feedback], " +
                "[aria-label*=copy i], [aria-label*=share i]"
              ).forEach((el) => el.remove());

              const content = String(
                clone.innerText || clone.textContent || ""
              );

              const body = String(document.body.innerText || "");
              const generating =
                /Stop generating|Generating\.\.\.|Building\.\.\.|Preview will appear/i
                  .test(body);

              return {
                cardCount: cards.length,
                reasoning,
                content,
                generating
              };
            }
            """
        )

    def _chatgpt_snapshot(self, page: Any) -> dict[str, Any]:
        return page.evaluate(
            r"""
            () => {
              const nodes = [
                ...document.querySelectorAll(
                  '[data-message-author-role="assistant"]'
                )
              ];
              const node = nodes.length ? nodes[nodes.length - 1] : null;
              if (!node) {
                return {count: 0, content: "", generating: false};
              }
              const clone = node.cloneNode(true);
              clone.querySelectorAll("button, [role=button]").forEach(
                (el) => el.remove()
              );
              const body = String(document.body.innerText || "");
              return {
                count: nodes.length,
                content: String(clone.innerText || clone.textContent || ""),
                generating: /Stop generating/i.test(body)
              };
            }
            """
        )

    def _submit_prompt(self, page: Any, prompt: str) -> None:
        composer = self._find_composer(page)
        composer.fill(prompt)
        composer.press("Enter")

    def _op_arena(
        self,
        messages: list[dict[str, Any]],
        mode: str,
        model: str,
        callback: Callable[[str, str], None] | None = None,
    ) -> BrowserResult:
        settings = self._settings("arena")
        base_url = str(settings.get("base_url") or "https://arena.ai").rstrip("/")
        mode = "code" if mode == "code" else "text"

        item = get_model(mode, model)
        if item is None:
            raise RuntimeError(
                f"Unknown Arena {mode} model '{model}'. "
                "It is not present in arena_models_live.json."
            )

        route = get_route(mode, model) or model
        path = "/code/direct" if mode == "code" else "/direct"
        arena_url = f"{base_url}{path}?model_a={quote(route, safe='')}"

        page = self._page("arena")
        self._goto(page, arena_url)
        self._wait_for_human_verification(page, "arena")

        parsed = parse_qs(urlparse(page.url).query)
        selected = str((parsed.get("model_a") or [""])[0]).strip()
        if selected.lower() == "max":
            mark_unverified(mode, model)
            raise RuntimeError(
                f"Arena redirected model '{model}' to the Max router. "
                "TurnToAPI refused the substitution."
            )

        prompt = _messages_to_prompt(messages)
        if not prompt.strip():
            raise ValueError("No prompt text was supplied.")

        initial = self._arena_snapshot(page)
        initial_count = int(initial.get("cardCount", 0))
        baseline_reasoning = _clean_text(
            str(initial.get("reasoning") or ""),
            reasoning=True,
        )
        baseline_content = _clean_text(str(initial.get("content") or ""))

        self._submit_prompt(page, prompt)

        timeout = float(settings.get("generation_timeout_seconds", 900))
        deadline = time.monotonic() + timeout
        last_reasoning = baseline_reasoning
        last_content = baseline_content
        stable_since: float | None = None
        saw_response = False

        while time.monotonic() < deadline:
            if self._is_security(page):
                self._wait_for_human_verification(page, "arena")

            snap = self._arena_snapshot(page)
            count = int(snap.get("cardCount", 0))
            reasoning = _clean_text(
                str(snap.get("reasoning") or ""),
                reasoning=True,
            )
            content = _clean_text(str(snap.get("content") or ""))

            response_started = (
                count > initial_count
                or reasoning != baseline_reasoning
                or content != baseline_content
            )
            if not response_started:
                time.sleep(0.06)
                continue

            saw_response = True

            if callback and (reasoning != last_reasoning or content != last_content):
                callback(reasoning, content)

            changed = reasoning != last_reasoning or content != last_content
            if changed:
                last_reasoning = reasoning
                last_content = content
                stable_since = time.monotonic()
            elif stable_since is None:
                stable_since = time.monotonic()

            generating = bool(snap.get("generating"))
            if (
                saw_response
                and last_content
                and not generating
                and stable_since is not None
                and (time.monotonic() - stable_since) >= 1.25
            ):
                mark_verified(mode, model, route=route)
                return BrowserResult(
                    content=last_content,
                    reasoning_content=last_reasoning,
                    final_url=page.url,
                )

            time.sleep(0.06)

        raise TimeoutError(
            f"Arena generation exceeded {int(timeout)} seconds."
        )

    def _op_chatgpt(
        self,
        messages: list[dict[str, Any]],
        callback: Callable[[str, str], None] | None = None,
    ) -> BrowserResult:
        settings = self._settings("chatgpt")
        if not bool(settings.get("enabled", False)):
            raise RuntimeError(
                "ChatGPT web adapter is disabled in config.yaml."
            )

        base_url = str(
            settings.get("base_url") or "https://chatgpt.com"
        ).rstrip("/")
        page = self._page("chatgpt")
        self._goto(page, base_url)
        self._wait_for_human_verification(page, "chatgpt")

        prompt = _messages_to_prompt(messages)
        before = self._chatgpt_snapshot(page)
        before_count = int(before.get("count", 0))
        self._submit_prompt(page, prompt)

        timeout = float(settings.get("generation_timeout_seconds", 900))
        deadline = time.monotonic() + timeout
        last = ""
        stable_since: float | None = None

        while time.monotonic() < deadline:
            if self._is_security(page):
                self._wait_for_human_verification(page, "chatgpt")

            snap = self._chatgpt_snapshot(page)
            count = int(snap.get("count", 0))
            content = _clean_text(str(snap.get("content") or ""))

            if callback and content != last:
                callback("", content)

            if content != last:
                last = content
                stable_since = time.monotonic()

            if (
                count > before_count
                and last
                and not bool(snap.get("generating"))
                and stable_since is not None
                and time.monotonic() - stable_since >= 1.25
            ):
                return BrowserResult(content=last, final_url=page.url)

            time.sleep(0.08)

        raise TimeoutError(
            f"ChatGPT web generation exceeded {int(timeout)} seconds."
        )


class BrowserWebAdapter:
    _worker: _BrowserWorker | None = None
    _worker_lock = threading.Lock()

    def __init__(
        self,
        provider: str,
        mode: str = "text",
        model: str | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.provider = provider
        self.mode = mode
        self.model = model
        self.config = config or {}

    @classmethod
    def configure_worker(cls, config: dict[str, Any]) -> None:
        with cls._worker_lock:
            if cls._worker is None:
                cls._worker = _BrowserWorker(config)

    def _get_worker(self) -> _BrowserWorker:
        self.configure_worker(self.config)
        assert self.__class__._worker is not None
        return self.__class__._worker

    def complete(
        self,
        messages: list[dict[str, Any]],
        callback: Callable[[str, str], None] | None = None,
    ) -> BrowserResult:
        worker = self._get_worker()

        if self.provider == "arena":
            if not self.model:
                raise ValueError("Arena model is required.")
            return worker.call(
                "arena",
                messages,
                self.mode,
                self.model,
                callback=callback,
            )

        if self.provider == "chatgpt":
            return worker.call(
                "chatgpt",
                messages,
                callback=callback,
            )

        raise ValueError(f"Unsupported browser provider: {self.provider}")


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = Path(path or ROOT / "config.yaml")
    if not path.exists():
        example = ROOT / "config.example.yaml"
        if example.exists():
            path = example
        else:
            return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        return yaml.safe_load(handle) or {}
