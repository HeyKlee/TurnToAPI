import asyncio
import os
import re
import time
import shutil
import uuid
from pathlib import Path
from urllib.parse import quote
from typing import Any, AsyncGenerator, Dict, List, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options

from selenium.common.exceptions import (
    StaleElementReferenceException,
    ElementClickInterceptedException,
    WebDriverException,
)


ROOT = Path(__file__).resolve().parent
PROFILE_ROOT = ROOT / "firefox_profiles"
DEBUG_ROOT = ROOT / "browser_debug"

PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
DEBUG_ROOT.mkdir(parents=True, exist_ok=True)


CHATGPT_COMPOSERS = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'textarea[placeholder*="Message"]',
    '[contenteditable="true"][role="textbox"]',
    "textarea",
]

ARENA_COMPOSERS = [
    "textarea",
    '[contenteditable="true"][role="textbox"]',
    '[contenteditable="true"]',
]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role", ""))

    return str(
        getattr(msg, "role", "")
    )


def _content(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(
            msg.get("content", "") or ""
        )

    return str(
        getattr(msg, "content", "") or ""
    )


def _messages_to_prompt(messages: List[Any]) -> str:
    messages = [
        m
        for m in messages
        if _content(m).strip()
    ]

    if not messages:
        return ""

    if (
        len(messages) == 1
        and _role(messages[0]) == "user"
    ):
        return _content(messages[0])

    names = {
        "system": "System",
        "user": "User",
        "assistant": "Assistant",
        "tool": "Tool",
    }

    result = [
        "Continue this conversation.",
        "Respond to the latest user message.",
        "",
    ]

    for msg in messages[-20:]:
        role = _role(msg)

        result.append(
            f"{names.get(role, role.title())}: "
            f"{_content(msg)}"
        )

    return "\n\n".join(result)


def _normalise(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]",
        "",
        str(value).lower(),
    )


def _tokens(value: str):
    return set(
        re.findall(
            r"[a-z0-9]+",
            str(value).lower(),
        )
    )


def _fresh_element(
    driver,
    selectors,
    timeout=10,
):
    deadline = time.time() + timeout

    while time.time() < deadline:

        for selector in selectors:

            try:
                elements = driver.find_elements(
                    By.CSS_SELECTOR,
                    selector,
                )

                for element in elements:

                    try:
                        if element.is_displayed():
                            return element

                    except StaleElementReferenceException:
                        continue

            except WebDriverException:
                pass

        time.sleep(0.2)

    return None


def _fresh_elements(
    driver,
    selector,
):
    try:
        elements = driver.find_elements(
            By.CSS_SELECTOR,
            selector,
        )
    except Exception:
        return []

    result = []

    for element in elements:

        try:
            if element.is_displayed():
                result.append(element)

        except StaleElementReferenceException:
            pass

    return result


def _fill_fresh(
    driver,
    selectors,
    text,
    timeout=10,
):
    deadline = time.time() + timeout
    last_error = None

    while time.time() < deadline:

        element = _fresh_element(
            driver,
            selectors,
            timeout=1,
        )

        if not element:
            continue

        try:
            element.click()

            element.send_keys(
                Keys.CONTROL,
                "a",
            )

            element.send_keys(
                Keys.DELETE,
            )

            element.send_keys(text)

            return True

        except StaleElementReferenceException as exc:
            # React/Svelte/etc rebuilt the textbox.
            last_error = exc
            time.sleep(0.15)

        except WebDriverException as exc:
            last_error = exc
            time.sleep(0.15)

    if last_error:
        raise last_error

    return False


def _press_enter_fresh(
    driver,
    selectors,
):
    element = _fresh_element(
        driver,
        selectors,
        timeout=5,
    )

    if not element:
        return False

    try:
        element.send_keys(
            Keys.ENTER
        )
        return True

    except StaleElementReferenceException:

        element = _fresh_element(
            driver,
            selectors,
            timeout=2,
        )

        if element:
            element.send_keys(
                Keys.ENTER
            )
            return True

    return False


def _click_fresh(
    driver,
    selectors,
    timeout=4,
):
    deadline = time.time() + timeout

    while time.time() < deadline:

        element = _fresh_element(
            driver,
            selectors,
            timeout=1,
        )

        if not element:
            continue

        try:
            element.click()
            return True

        except (
            StaleElementReferenceException,
            ElementClickInterceptedException,
        ):
            time.sleep(0.15)

    return False


def _safe_text(element):
    try:
        return element.text.strip()

    except StaleElementReferenceException:
        return ""


def _candidate_texts(driver):
    selectors = [
        '[data-testid*="response"]',
        '[data-testid*="message"]',
        '[class*="response"]',
        ".prose",
        ".markdown",
        "article",
    ]

    best = []

    for selector in selectors:

        values = []

        for element in _fresh_elements(
            driver,
            selector,
        ):
            text = _safe_text(element)

            if text:
                values.append(text)

        if len(values) > len(best):
            best = values

    return best



# TURNTOAPI_LIVE_DOM_OBSERVER_V1_BEGIN

#
# V2 transport:
#
# Arena JavaScript NEVER contacts localhost.
#
# The MutationObserver stores the latest clean response state in:
#
#     window.__turnToApiLatest
#
# A Python watcher reads that value through WebDriver and forwards
# it to TurnToAPI's loopback bridge.
#

import json as _turntoapi_json
import threading as _turntoapi_threading
import time as _turntoapi_time
import urllib.request as _turntoapi_urllib_request


_TURNTOAPI_WATCHER_LOCK = (
    _turntoapi_threading.Lock()
)

_TURNTOAPI_WATCHERS = {}


def _turntoapi_stop_existing_watcher(
    driver,
):

    try:
        key = str(
            driver.session_id
            or ""
        )

    except Exception:
        return


    if not key:
        return


    with _TURNTOAPI_WATCHER_LOCK:

        old = _TURNTOAPI_WATCHERS.pop(
            key,
            None,
        )


    if old:

        try:
            old.set()

        except Exception:
            pass


def _turntoapi_bridge_session():

    try:

        with _turntoapi_urllib_request.urlopen(
            "http://127.0.0.1:8765/session",
            timeout=0.25,
        ) as response:

            return (
                response.read()
                .decode(
                    "utf-8",
                    "replace",
                )
                .strip()
            )

    except Exception:
        return ""


def _turntoapi_post_snapshot(
    payload,
):

    try:

        raw = (
            _turntoapi_json.dumps(
                payload,
                ensure_ascii=False,
                separators=(
                    ",",
                    ":",
                ),
            )
            .encode(
                "utf-8"
            )
        )

        request = (
            _turntoapi_urllib_request.Request(
                "http://127.0.0.1:8765/event",
                data=raw,
                method="POST",
                headers={
                    "Content-Type":
                        "application/json",
                },
            )
        )

        with _turntoapi_urllib_request.urlopen(
            request,
            timeout=0.25,
        ):
            pass

        return True

    except Exception:
        return False


def _turntoapi_start_python_watcher(
    driver,
    session_id,
):

    try:

        key = str(
            driver.session_id
            or ""
        )

    except Exception:
        return False


    if not key:
        return False


    _turntoapi_stop_existing_watcher(
        driver
    )


    stop_event = (
        _turntoapi_threading.Event()
    )


    with _TURNTOAPI_WATCHER_LOCK:

        _TURNTOAPI_WATCHERS[
            key
        ] = stop_event


    def watcher():

        last_serialized = ""

        failures = 0

        active_checks = 0


        try:

            while not stop_event.is_set():

                #
                # Roughly every 250 ms confirm that this SSE request
                # is still the active TurnToAPI session.
                #

                active_checks += 1

                if active_checks >= 4:

                    active_checks = 0

                    current_session = (
                        _turntoapi_bridge_session()
                    )

                    if (
                        not current_session
                        or
                        current_session
                        != session_id
                    ):
                        break


                try:

                    serialized = (
                        driver.execute_script(
                            r"""
                            try {

                                if (
                                    !window.__turnToApiLatest
                                ) {
                                    return "";
                                }

                                return JSON.stringify(
                                    window.__turnToApiLatest
                                );

                            } catch (_) {

                                return "";
                            }
                            """
                        )
                        or ""
                    )


                    failures = 0


                except Exception:

                    failures += 1

                    if failures >= 30:
                        break

                    _turntoapi_time.sleep(
                        0.075
                    )

                    continue


                if (
                    serialized
                    and
                    serialized
                    != last_serialized
                ):

                    try:

                        payload = (
                            _turntoapi_json.loads(
                                serialized
                            )
                        )


                        if (
                            str(
                                payload.get(
                                    "session",
                                    "",
                                )
                            )
                            == session_id
                        ):

                            if _turntoapi_post_snapshot(
                                payload
                            ):

                                last_serialized = (
                                    serialized
                                )


                    except Exception:
                        pass


                #
                # 60 ms WebDriver sampling.
                #
                # DOM mutations themselves are debounced at 20 ms.
                #

                _turntoapi_time.sleep(
                    0.060
                )


        finally:

            with _TURNTOAPI_WATCHER_LOCK:

                current = (
                    _TURNTOAPI_WATCHERS.get(
                        key
                    )
                )

                if current is stop_event:

                    _TURNTOAPI_WATCHERS.pop(
                        key,
                        None,
                    )


    thread = (
        _turntoapi_threading.Thread(
            target=watcher,
            name=(
                "TurnToAPI-Arena-DOM-"
                + key[:8]
            ),
            daemon=True,
        )
    )

    thread.start()

    return True


def _turntoapi_install_live_observer(
    driver,
):

    """
    Install a MutationObserver into Arena.

    Browser JavaScript stores data only in browser memory.
    Python/WebDriver transports the snapshots to localhost.
    """

    try:

        session_id = (
            _turntoapi_bridge_session()
        )


        if not session_id:
            return False


        script = r"""
        const SESSION =
            String(
                arguments[0]
                ||
                ""
            );


        if (!SESSION) {
            return false;
        }


        try {

            if (
                window.__turnToApiObserver
            ) {

                window
                    .__turnToApiObserver
                    .disconnect();
            }

        } catch (_) {}


        function visible(el) {

            if (!el) {
                return false;
            }


            const style =
                window.getComputedStyle(
                    el
                );


            if (
                style.display === "none"
                ||
                style.visibility === "hidden"
            ) {
                return false;
            }


            const rect =
                el.getBoundingClientRect();


            return (
                rect.width > 0
                &&
                rect.height > 0
            );
        }


        function rawText(el) {

            return String(
                (
                    el
                    &&
                    (
                        el.innerText
                        ||
                        el.textContent
                    )
                )
                ||
                ""
            )
            .replace(
                /\r/g,
                ""
            );
        }


        function cleanLines(
            value
        ) {

            const source =
                String(
                    value
                    ||
                    ""
                )
                .split(
                    /\n/
                );


            const output = [];

            let skipProvider =
                false;


            for (
                let raw
                of source
            ) {

                const line =
                    raw.trim();


                if (!line) {
                    continue;
                }


                if (
                    skipProvider
                ) {

                    skipProvider =
                        false;

                    continue;
                }


                if (
                    /^Thought for\s+\d+(?:\.\d+)?\s*seconds?$/i
                    .test(
                        line
                    )
                ) {
                    continue;
                }


                if (
                    /^Response provided by$/i
                    .test(
                        line
                    )
                ) {

                    skipProvider =
                        true;

                    continue;
                }


                if (
                    /^(Chat completed|Building\.\.\.|Generating\.\.\.|Stop generating)$/i
                    .test(
                        line
                    )
                ) {
                    continue;
                }


                if (
                    /^Preview will appear/i
                    .test(
                        line
                    )
                ) {
                    continue;
                }


                output.push(
                    line
                );
            }


            return output
                .join(
                    "\n"
                )
                .trim();
        }


        //
        // Record everything that already existed BEFORE submitting
        // the new prompt. New Arena response containers are preferred.
        //

        const baseline =
            new WeakSet(
                Array.from(
                    document.querySelectorAll(
                        "div.flex.flex-col.gap-3"
                    )
                )
            );


        function latestThought() {

            const all =
                Array.from(
                    document.querySelectorAll(
                        "div.not-prose[data-state]"
                    )
                );


            if (!all.length) {
                return null;
            }


            //
            // Prefer a newly added thought element.
            //

            const newOnes =
                all.filter(
                    el => {

                        let parent =
                            el;

                        while (parent) {

                            if (
                                parent.matches
                                &&
                                parent.matches(
                                    "div.flex.flex-col.gap-3"
                                )
                                &&
                                !baseline.has(
                                    parent
                                )
                            ) {
                                return true;
                            }

                            parent =
                                parent.parentElement;
                        }

                        return false;
                    }
                );


            const pool =
                newOnes.length
                ? newOnes
                : all;


            return pool[
                pool.length - 1
            ];
        }


        function responseContainer(
            thought
        ) {

            if (thought) {

                let node =
                    thought;


                for (
                    let i = 0;
                    i < 12
                    &&
                    node;
                    i++,
                    node =
                        node.parentElement
                ) {

                    if (
                        node.matches
                        &&
                        node.matches(
                            "div.flex.flex-col.gap-3"
                        )
                    ) {

                        return node;
                    }
                }
            }


            let candidates =
                Array.from(
                    document.querySelectorAll(
                        "div.flex.flex-col.gap-3"
                    )
                )
                .filter(
                    el =>
                        !baseline.has(
                            el
                        )
                        &&
                        rawText(
                            el
                        )
                            .trim()
                            .length > 0
                );


            let cards =
                candidates.filter(
                    el =>
                        el.closest(
                            "div.bg-surface-primary"
                        )
                        ||
                        el.closest(
                            "div.border-border-faint"
                        )
                );


            if (
                cards.length
            ) {

                candidates =
                    cards;
            }


            if (
                candidates.length
            ) {

                return candidates[
                    candidates.length - 1
                ];
            }


            //
            // Fallback for Arena reusing a pre-existing React node:
            // choose the final response-looking container rather than
            // document.body.
            //

            candidates =
                Array.from(
                    document.querySelectorAll(
                        "div.flex.flex-col.gap-3"
                    )
                )
                .filter(
                    el =>
                        rawText(
                            el
                        )
                            .trim()
                            .length > 0
                        &&
                        (
                            el.closest(
                                "div.bg-surface-primary"
                            )
                            ||
                            el.closest(
                                "div.border-border-faint"
                            )
                        )
                );


            return (
                candidates.length
                ? candidates[
                    candidates.length - 1
                  ]
                : null
            );
        }


        function buildState() {

            const thought =
                latestThought();


            let reasoning =
                "";


            if (thought) {

                reasoning =
                    cleanLines(
                        rawText(
                            thought
                        )
                    );
            }


            const container =
                responseContainer(
                    thought
                );


            let content =
                "";


            if (container) {

                const clone =
                    container.cloneNode(
                        true
                    );


                //
                // Remove reasoning before extracting final answer.
                //

                clone
                    .querySelectorAll(
                        "div.not-prose[data-state]"
                    )
                    .forEach(
                        element =>
                            element.remove()
                    );


                //
                // Remove response controls / input UI.
                //

                clone
                    .querySelectorAll(
                        [
                            "button",
                            "[role='button']",
                            "textarea",
                            "input",
                            "svg"
                        ].join(",")
                    )
                    .forEach(
                        element =>
                            element.remove()
                    );


                content =
                    cleanLines(
                        rawText(
                            clone
                        )
                    );
            }


            return {
                session:
                    SESSION,

                reasoning:
                    reasoning,

                content:
                    content,

                updated:
                    Date.now()
            };
        }


        let scheduled =
            false;


        function capture() {

            scheduled =
                false;


            try {

                window.__turnToApiLatest =
                    buildState();

            } catch (_) {}
        }


        function schedule() {

            if (scheduled) {
                return;
            }


            scheduled =
                true;


            setTimeout(
                capture,
                20
            );
        }


        window.__turnToApiLatest = {
            session:
                SESSION,

            reasoning:
                "",

            content:
                "",

            updated:
                Date.now()
        };


        window.__turnToApiObserver =
            new MutationObserver(
                schedule
            );


        window.__turnToApiObserver
            .observe(
                document.body,
                {
                    subtree:
                        true,

                    childList:
                        true,

                    characterData:
                        true,

                    attributes:
                        true,

                    attributeFilter: [
                        "data-state",
                        "aria-expanded",
                        "class"
                    ]
                }
            );


        schedule();


        return true;
        """


        installed = bool(
            driver.execute_script(
                script,
                session_id,
            )
        )


        if not installed:
            return False


        return (
            _turntoapi_start_python_watcher(
                driver,
                session_id,
            )
        )


    except Exception:

        return False


# TURNTOAPI_LIVE_DOM_OBSERVER_V1_END


# TURNTOAPI_ARENA_PLAYWRIGHT_V1_BEGIN

#
# Arena browser creation is intercepted only while BrowserWebAdapter._arena()
# is on the call stack.
#
# All other Selenium Firefox use remains unchanged.
#

try:

    from arena_playwright_shim import (
        install_into_adapter as
        _turntoapi_install_playwright_arena,
    )

    _turntoapi_install_playwright_arena(
        globals()
    )

except Exception as _turntoapi_playwright_error:

    import logging as _turntoapi_logging

    _turntoapi_logging.warning(
        "Could not install Arena Playwright browser shim: %r",
        _turntoapi_playwright_error,
    )

# TURNTOAPI_ARENA_PLAYWRIGHT_V1_END

class BrowserWebAdapter:

    _locks: Dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        provider: str,
        mode: str = "direct",
        model: Optional[str] = None,
    ):
        self.provider = provider
        self.mode = mode
        self.model = model

        self.lock = self._locks.setdefault(
            provider,
            asyncio.Lock(),
        )

    async def chat(
        self,
        messages,
        stream: bool,
        options: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:

        prompt = _messages_to_prompt(
            messages
        )

        if not prompt:
            yield "No user prompt supplied."
            return

        async with self.lock:

            try:
                response = await asyncio.to_thread(
                    self._run_sync,
                    prompt,
                )

            except Exception as exc:
                response = (
                    f"{self.provider} Firefox adapter error: "
                    f"{type(exc).__name__}: {exc}"
                )

        if not stream:
            yield response
            return

        for piece in re.split(
            r"(\s+)",
            response,
        ):
            if piece:
                yield piece
                await asyncio.sleep(0)

    def _make_runtime_profile(self):
        """
        Never launch Firefox directly against the authenticated
        master profile.

        Firefox profiles cannot safely be shared by multiple
        Firefox processes. Instead:

        master authenticated profile
                ↓
        fresh per-request clone
                ↓
        headless Selenium Firefox
                ↓
        delete clone when finished
        """

        master = (
            PROFILE_ROOT
            / self.provider
        )

        if not master.exists():
            raise RuntimeError(
                f"Firefox master profile missing: {master}"
            )

        runtime_root = (
            ROOT
            / "firefox_runtime"
        )

        runtime_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        runtime = (
            runtime_root
            / (
                self.provider
                + "-"
                + uuid.uuid4().hex
            )
        )

        def ignore_profile_files(
            directory,
            names,
        ):
            ignored = []

            skip_names = {
                "parent.lock",
                "lock",
                ".parentlock",
                "cache2",
                "startupCache",
                "shader-cache",
                "crashes",
                "minidumps",
                "saved-telemetry-pings",
                "datareporting",
            }

            for name in names:

                if name in skip_names:
                    ignored.append(name)
                    continue

                if name.endswith(".lock"):
                    ignored.append(name)

            return ignored

        shutil.copytree(
            master,
            runtime,
            ignore=ignore_profile_files,
        )

        # Absolutely ensure no copied lock survives.
        for lock_name in (
            "parent.lock",
            "lock",
            ".parentlock",
        ):
            try:
                (
                    runtime
                    / lock_name
                ).unlink()
            except FileNotFoundError:
                pass

        return runtime


    def _make_runtime_profile(self):
        """
        Never launch Firefox directly against the authenticated
        master profile.

        Firefox profiles cannot safely be shared by multiple
        Firefox processes. Instead:

        master authenticated profile
                ↓
        fresh per-request clone
                ↓
        headless Selenium Firefox
                ↓
        delete clone when finished
        """

        master = (
            PROFILE_ROOT
            / self.provider
        )

        if not master.exists():
            raise RuntimeError(
                f"Firefox master profile missing: {master}"
            )

        runtime_root = (
            ROOT
            / "firefox_runtime"
        )

        runtime_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        runtime = (
            runtime_root
            / (
                self.provider
                + "-"
                + uuid.uuid4().hex
            )
        )

        def ignore_profile_files(
            directory,
            names,
        ):
            ignored = []

            skip_names = {
                "parent.lock",
                "lock",
                ".parentlock",
                "cache2",
                "startupCache",
                "shader-cache",
                "crashes",
                "minidumps",
                "saved-telemetry-pings",
                "datareporting",
            }

            for name in names:

                if name in skip_names:
                    ignored.append(name)
                    continue

                if name.endswith(".lock"):
                    ignored.append(name)

            return ignored

        shutil.copytree(
            master,
            runtime,
            ignore=ignore_profile_files,
        )

        # Absolutely ensure no copied lock survives.
        for lock_name in (
            "parent.lock",
            "lock",
            ".parentlock",
        ):
            try:
                (
                    runtime
                    / lock_name
                ).unlink()
            except FileNotFoundError:
                pass

        return runtime


    def _driver(self):

        options = Options()

        # ========================================================
        # ARENA
        #
        # Arena currently challenges the headless browser with
        # reCAPTCHA/security verification.
        #
        # We therefore run Arena in a normal headed Firefox using
        # its own dedicated authenticated profile.
        #
        # -no-remote + -new-instance means the user's ordinary
        # Firefox can remain running.
        # ========================================================

        if self.provider == "arena":

            os.environ.pop(
                "MOZ_HEADLESS",
                None,
            )

            profile = (
                PROFILE_ROOT
                / "arena"
            )

            if not profile.exists():
                raise RuntimeError(
                    f"Arena Firefox profile missing: {profile}"
                )

            options.add_argument(
                "-no-remote"
            )

            options.add_argument(
                "-new-instance"
            )

            options.add_argument(
                "-profile"
            )

            options.add_argument(
                str(profile)
            )

            options.set_preference(
                "dom.webnotifications.enabled",
                False,
            )

            options.set_preference(
                "browser.shell.checkDefaultBrowser",
                False,
            )

            driver = webdriver.Firefox(
                options=options
            )

            # Arena uses the persistent master profile directly.
            driver._turntoapi_runtime_profile = None

        # ========================================================
        # CHATGPT
        #
        # Keep the existing isolated/headless design.
        # ========================================================

        else:

            runtime_profile = (
                self._make_runtime_profile()
            )

            os.environ[
                "MOZ_HEADLESS"
            ] = "1"

            options.add_argument(
                "-headless"
            )

            options.add_argument(
                "-no-remote"
            )

            options.add_argument(
                "-new-instance"
            )

            options.add_argument(
                "-profile"
            )

            options.add_argument(
                str(runtime_profile)
            )

            options.set_preference(
                "dom.webnotifications.enabled",
                False,
            )

            options.set_preference(
                "browser.shell.checkDefaultBrowser",
                False,
            )

            try:
                driver = webdriver.Firefox(
                    options=options
                )

            except Exception:

                shutil.rmtree(
                    runtime_profile,
                    ignore_errors=True,
                )

                raise

            driver._turntoapi_runtime_profile = str(
                runtime_profile
            )

        try:
            driver.command_executor.client_config.timeout = int(
                os.environ.get(
                    "TURNTOAPI_WEBDRIVER_TIMEOUT",
                    "900",
                )
            )
        except Exception:
            pass

        driver.set_page_load_timeout(
            90
        )

        driver.set_script_timeout(
            120
        )

        return driver


    def _close_driver(
        self,
        driver,
    ):
        """
        Close Firefox and remove its temporary cloned profile.
        """

        runtime = getattr(
            driver,
            "_turntoapi_runtime_profile",
            None,
        )

        try:
            driver.quit()

        except Exception:
            pass

        if runtime:

            # Give Firefox/geckodriver a moment to release files.
            for _ in range(20):

                try:
                    shutil.rmtree(
                        runtime
                    )

                    break

                except Exception:
                    time.sleep(0.15)

            else:
                shutil.rmtree(
                    runtime,
                    ignore_errors=True,
                )


    def _run_sync(
        self,
        prompt: str,
    ) -> str:

        if self.provider == "chatgpt":
            return self._chatgpt(
                prompt
            )

        if self.provider == "arena":
            return self._arena(
                prompt
            )

        raise RuntimeError(
            f"Unsupported provider: {self.provider}"
        )

    # ============================================================
    # ChatGPT
    # ============================================================

    def _chatgpt(
        self,
        prompt: str,
    ) -> str:

        driver = self._driver()

        try:
            driver.get(
                "https://chatgpt.com/"
            )

            composer = _fresh_element(
                driver,
                CHATGPT_COMPOSERS,
                timeout=15,
            )

            if not composer:
                driver.save_screenshot(
                    str(
                        DEBUG_ROOT
                        / "chatgpt-no-composer.png"
                    )
                )

                raise RuntimeError(
                    "ChatGPT composer was not found."
                )

            assistant_selector = (
                '[data-message-author-role="assistant"]'
            )

            before = len(
                _fresh_elements(
                    driver,
                    assistant_selector,
                )
            )

            # Never retain composer reference while typing.
            _fill_fresh(
                driver,
                CHATGPT_COMPOSERS,
                prompt,
                timeout=10,
            )

            # Allow UI to rebuild itself.
            time.sleep(0.5)

            # Re-find send button AFTER the DOM update.
            sent = _click_fresh(
                driver,
                [
                    '[data-testid="send-button"]',
                    'button[aria-label*="Send"]',
                    'button[aria-label*="send"]',
                ],
                timeout=3,
            )

            if not sent:

                # Re-find composer again.
                if not _press_enter_fresh(
                    driver,
                    CHATGPT_COMPOSERS,
                ):
                    raise RuntimeError(
                        "Could not submit ChatGPT prompt."
                    )

            deadline = time.time() + 150

            # Wait for a new assistant message.
            while time.time() < deadline:

                responses = _fresh_elements(
                    driver,
                    assistant_selector,
                )

                if len(responses) > before:
                    break

                time.sleep(0.35)

            last = ""
            stable = 0

            while time.time() < deadline:

                responses = _fresh_elements(
                    driver,
                    assistant_selector,
                )

                text = ""

                if responses:
                    # Fresh lookup every pass.
                    text = _safe_text(
                        responses[-1]
                    )

                if text:

                    if text == last:
                        stable += 1
                    else:
                        stable = 0

                    last = text

                    if stable >= 5:
                        return last

                time.sleep(0.45)

            if last:
                return last

            driver.save_screenshot(
                str(
                    DEBUG_ROOT
                    / "chatgpt-no-response.png"
                )
            )

            raise RuntimeError(
                "ChatGPT did not produce a readable response."
            )

        finally:
            self._close_driver(driver)

    # ============================================================
    # Arena
    # ============================================================

    def _arena(
        self,
        prompt: str,
    ) -> str:

        driver = self._driver()

        try:

            if self.mode == "agent":
                arena_url = "https://arena.ai/agent"

            elif (
                self.mode in {
                    "direct",
                    "text",
                    "code",
                }
                and self.model
            ):
                # Direct Arena URLs accept the model through model_a.
                #
                # Examples:
                # /text/direct?model_a=gemini-4-argon-high
                # /code/direct?model_a=gemma-4-31b

                modality = (
                    "code"
                    if self.mode == "code"
                    else "text"
                )

                arena_url = (
                    f"https://arena.ai/{modality}/direct"
                    f"?model_a={quote(self.model, safe='')}"
                )

            elif self.mode == "max":
                arena_url = (
                    "https://arena.ai/text/direct"
                    "?model_a=max"
                )

            else:
                arena_url = "https://arena.ai/"

            # =====================================================
            # TURNTOAPI_ARENA_REGISTRY_ROUTE_V1
            #
            # A selector-discovered model can have a public label that
            # differs from the direct-route ID Arena actually uses.
            #
            # Once resolved, use the stored route on future requests.
            # =====================================================

            _turntoapi_api_model = str(
                self.model or ""
            ).strip()

            _turntoapi_mode = (
                "code"
                if self.mode == "code"
                else "text"
            )

            if (
                _turntoapi_api_model
                and "/direct" in arena_url
                and "model_a=" in arena_url
            ):
                try:
                    from arena_model_registry import get_route

                    _turntoapi_route_model = get_route(
                        _turntoapi_mode,
                        _turntoapi_api_model,
                    )

                    if (
                        _turntoapi_route_model
                        and
                        _turntoapi_route_model
                        != _turntoapi_api_model
                    ):
                        arena_url = (
                            arena_url.split(
                                "model_a=",
                                1,
                            )[0]
                            + "model_a="
                            + quote(
                                _turntoapi_route_model,
                                safe="",
                            )
                        )

                except Exception:
                    pass

            driver.get(arena_url)

            # TURNTOAPI_LIVE_DOM_OBSERVER_CALL_V1
            _turntoapi_install_live_observer(driver)

            # =====================================================
            # TURNTOAPI_ARENA_TRIGGERED_SCANNER_V1
            #
            # Policy:
            #
            #  verified old model -> Max
            #      FULL selector refresh
            #
            #  newly discovered/unverified model -> Max
            #      resolve only that model through the selector
            #
            #  unknown arbitrary model -> Max
            #      fail closed; do not scan
            #
            # Never silently claim Max is the requested model.
            # =====================================================

            if (
                _turntoapi_api_model
                and "/direct" in arena_url
                and "model_a=" in arena_url
            ):

                time.sleep(
                    1.5
                )

                _turntoapi_current_url = str(
                    driver.current_url
                    or ""
                )

                _turntoapi_actual_model = ""

                if "model_a=" in _turntoapi_current_url:

                    _turntoapi_actual_model = (
                        _turntoapi_current_url
                        .split(
                            "model_a=",
                            1,
                        )[1]
                        .split(
                            "&",
                            1,
                        )[0]
                        .strip()
                    )

                try:
                    from arena_model_registry import (
                        get_entry,
                        mark_working,
                        resolve_single_model,
                        sync_on_verified_max,
                    )

                    _turntoapi_entry = get_entry(
                        _turntoapi_mode,
                        _turntoapi_api_model,
                    )

                except Exception:
                    _turntoapi_entry = None

                # Direct route worked.
                if (
                    _turntoapi_actual_model
                    and _turntoapi_actual_model.lower()
                    != "max"
                ):

                    if _turntoapi_entry:
                        try:
                            mark_working(
                                _turntoapi_mode,
                                _turntoapi_api_model,
                                _turntoapi_actual_model,
                            )
                        except Exception:
                            pass

                # Arena silently routed us to Max.
                elif (
                    _turntoapi_actual_model.lower()
                    == "max"
                ):

                    if not _turntoapi_entry:

                        raise RuntimeError(
                            (
                                "Arena redirected unknown model '"
                                + _turntoapi_api_model
                                + "' to the Max router. "
                                + "TurnToAPI refused the substitution. "
                                + "No registry scan was triggered because "
                                + "this model was not previously known."
                            )
                        )

                    _turntoapi_verified_before = bool(
                        _turntoapi_entry.get(
                            "verified"
                        )
                    )

                    if _turntoapi_verified_before:

                        _turntoapi_scan = (
                            sync_on_verified_max(
                                driver,
                                _turntoapi_mode,
                                _turntoapi_api_model,
                            )
                        )

                    else:

                        _turntoapi_scan = (
                            resolve_single_model(
                                driver,
                                _turntoapi_mode,
                                _turntoapi_api_model,
                            )
                        )

                    if not _turntoapi_scan.get(
                        "recovered"
                    ):

                        raise RuntimeError(
                            (
                                "Arena redirected model '"
                                + _turntoapi_api_model
                                + "' to Max. "
                                + (
                                    "The live Arena model list was refreshed, "
                                    if _turntoapi_verified_before
                                    else
                                    "The model was checked against Arena's selector, "
                                )
                                + "but the requested model could not be recovered. "
                                + "TurnToAPI refused to use Max."
                            )
                        )

                    # TURNTOAPI_LIVE_DOM_OBSERVER_RECOVERY_V1
                    _turntoapi_install_live_observer(driver)

                    # Scanner/selector has now left this exact browser
                    # on the recovered direct model, so normal Arena
                    # prompt handling can continue below.


            composer = _fresh_element(
                driver,
                ARENA_COMPOSERS,
                timeout=15,
            )

            if not composer:

                try:
                    body_now = driver.find_element(
                        By.TAG_NAME,
                        "body",
                    ).text
                except Exception:
                    body_now = ""

                security_check = (
                    "Security Verification" in body_now
                    or "reCAPTCHA" in body_now
                    or "security check" in body_now.lower()
                    or "cloudflare" in body_now.lower()
                    or "verify you are human" in body_now.lower()
                    or "performing security verification" in body_now.lower()
                    or "just a moment" in body_now.lower()
                )

                if security_check:

                    try:
                        (
                            DEBUG_ROOT
                            / "arena-security-verification.txt"
                        ).write_text(
                            body_now,
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                    # Arena has requested a real human verification.
                    #
                    # Leave the headed browser visible so the user can
                    # complete the verification manually.
                    #
                    # Do NOT automate or bypass it.
                    composer = _fresh_element(
                        driver,
                        ARENA_COMPOSERS,
                        timeout=600,
                    )

                if not composer:

                    driver.save_screenshot(
                        str(
                            DEBUG_ROOT
                            / "arena-no-composer.png"
                        )
                    )

                    raise RuntimeError(
                        "Arena composer was not found. "
                        "If Arena displayed a security verification, "
                        "complete it manually in the dedicated Arena "
                        "Firefox window and retry."
                    )

            # Verification/login is complete and the real composer
            # exists. Minimise the dedicated automation browser while
            # the model generates.
            try:
                driver.minimize_window()
            except Exception:
                pass

            if (
                self.mode in {
                    "direct",
                    "text",
                    "code",
                }
                and self.model
            ):
                # Model already selected through ?model_a= in URL.
                pass

            elif self.mode in {
                "direct",
                "text",
                "code",
            }:
                raise RuntimeError(
                    "A specific Arena model is required."
                )

            elif self.mode == "auto":

                self._click_text(
                    driver,
                    "Auto",
                )

            elif self.mode == "max":

                self._select_arena_model(
                    driver,
                    "Max",
                )

            elif self.mode in {
                "side-by-side",
                "side_by_side",
            }:

                if not (
                    self._click_text(
                        driver,
                        "Side by Side",
                    )
                    or
                    self._click_text(
                        driver,
                        "Side-by-Side",
                    )
                ):
                    raise RuntimeError(
                        "Arena Side-by-Side requires "
                        "two explicit model selections "
                        "in the current UI."
                    )

            # ====================================================
            # HEADLESS RESPONSE DETECTOR V2
            #
            # Arena's DOM differs enough between visible/headless
            # sessions that the old .prose/.response selector scan can
            # miss perfectly valid generations.
            #
            # Instead:
            #   1. Capture all visible page text BEFORE submission.
            #   2. Submit prompt.
            #   3. Capture page text repeatedly.
            #   4. Remove anything that existed before.
            #   5. Remove the user's own submitted prompt.
            #   6. Return newly-generated stable text.
            # ====================================================

            def page_text():
                try:
                    value = driver.execute_script(
                        """
                        return document.body
                            ? document.body.innerText
                            : "";
                        """
                    )

                    return str(
                        value or ""
                    )

                except Exception:
                    try:
                        return driver.find_element(
                            By.TAG_NAME,
                            "body",
                        ).text

                    except Exception:
                        return ""


            def clean_lines(value):
                result = []

                ignored_exact = {
                    "",
                    "battle",
                    "auto",
                    "direct",
                    "code",
                    "chat",
                    "work",
                    "send",
                    "stop",
                    "retry",
                    "copy",
                    "share",
                    "edit",
                    "regenerate",
                    "regenerate response",
                    "good response",
                    "bad response",
                    "model a",
                    "model b",
                }

                for raw in value.splitlines():

                    line = raw.strip()

                    if not line:
                        continue

                    if line.lower() in ignored_exact:
                        continue

                    result.append(
                        line
                    )

                return result


            before_body = page_text()

            before_lines = set(
                clean_lines(
                    before_body
                )
            )

            submitted_prompt = (
                prompt.strip()
            )

            _fill_fresh(
                driver,
                ARENA_COMPOSERS,
                submitted_prompt,
                timeout=10,
            )

            time.sleep(0.5)

            sent = _click_fresh(
                driver,
                [
                    'button[aria-label*="Send"]',
                    'button[aria-label*="send"]',
                    'button[type="submit"]',
                ],
                timeout=3,
            )

            if not sent:

                if not _press_enter_fresh(
                    driver,
                    ARENA_COMPOSERS,
                ):
                    raise RuntimeError(
                        "Could not submit Arena prompt."
                    )

            generation_timeout = int(
                os.environ.get(
                    "TURNTOAPI_ARENA_GENERATION_TIMEOUT",
                    "900",
                )
            )

            deadline = (
                time.time()
                + generation_timeout
            )

            answer = ""
            previous = ""
            stable = 0
            iteration = 0

            while time.time() < deadline:

                iteration += 1

                body = page_text()

                # =================================================
                # ARENA CODE BUSY STATE
                #
                # Do not mistake progress/status text for the final
                # model output.
                # =================================================

                body_lower = body.lower()

                arena_busy = (
                    "building..." in body_lower
                    or "preview will appear when agent is done working"
                    in body_lower
                    or "generating..." in body_lower
                    or "stop generating" in body_lower
                )

                if arena_busy:

                    try:
                        (
                            DEBUG_ROOT
                            / "arena-building-live.txt"
                        ).write_text(
                            body,
                            encoding="utf-8",
                        )

                        (
                            DEBUG_ROOT
                            / "arena-current-url.txt"
                        ).write_text(
                            driver.current_url,
                            encoding="utf-8",
                        )

                    except Exception:
                        pass

                    # Reset stability so a status screen can never
                    # accidentally satisfy the final-answer detector.
                    answer = ""
                    previous = ""
                    stable = 0

                    time.sleep(0.75)
                    continue

                # Arena may interrupt a session with a human security
                # verification. This is not model output.
                if (
                    "Security Verification" in body
                    or "reCAPTCHA" in body
                    or "security check" in body.lower()
                    or "cloudflare" in body.lower()
                    or "verify you are human" in body.lower()
                    or "performing security verification" in body.lower()
                    or "just a moment" in body.lower()
                ):
                    try:
                        driver.maximize_window()
                    except Exception:
                        pass

                    try:
                        (
                            DEBUG_ROOT
                            / "arena-security-verification.txt"
                        ).write_text(
                            body,
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                    # Wait for the human to complete it.
                    time.sleep(1)
                    continue

                # Continuously save exactly what headless Firefox sees.
                try:
                    (
                        DEBUG_ROOT
                        / "arena-headless-live.txt"
                    ).write_text(
                        body,
                        encoding="utf-8",
                    )

                    (
                        DEBUG_ROOT
                        / "arena-current-url.txt"
                    ).write_text(
                        driver.current_url,
                        encoding="utf-8",
                    )

                    if iteration == 20:
                        driver.save_screenshot(
                            str(
                                DEBUG_ROOT
                                / "arena-headless-10s.png"
                            )
                        )

                except Exception:
                    pass

                lines = clean_lines(
                    body
                )

                new_lines = []

                prompt_seen = False

                for line in lines:

                    # Start paying strongest attention after Arena has
                    # rendered the user's submitted message.
                    if line == submitted_prompt:
                        prompt_seen = True
                        continue

                    # Never echo the user's own prompt.
                    if line == submitted_prompt:
                        continue

                    if (
                        submitted_prompt
                        and line.startswith(
                            submitted_prompt
                        )
                    ):
                        remainder = line[
                            len(submitted_prompt):
                        ].strip(
                            " \t\r\n:-"
                        )

                        if remainder:
                            line = remainder
                        else:
                            continue

                    # Anything already visible before submitting isn't
                    # the new model response.
                    if line in before_lines:
                        continue

                    low = line.lower()

                    if (
                        low in {
                            "thinking",
                            "generating",
                            "generating...",
                            "building",
                            "building...",
                            "stop generating",
                            "cancel",
                            "download",
                            "preview will appear when agent is done working",
                        }
                        or low.startswith("generating")
                        or low.startswith("building")
                        or "preview will appear when agent is done working"
                        in low
                        or "inputs are processed by third-party ai" in low
                    ):
                        continue

                    if (
                        line
                        and line not in new_lines
                    ):
                        new_lines.append(
                            line
                        )

                if new_lines:

                    candidate = "\n".join(
                        new_lines
                    ).strip()

                    # A response must not simply be the user's prompt.
                    if (
                        candidate
                        and candidate != submitted_prompt
                    ):
                        answer = candidate

                if answer:

                    if answer == previous:
                        stable += 1
                    else:
                        previous = answer
                        stable = 0

                    # 3 seconds unchanged is enough for the tiny test,
                    # while longer responses naturally keep resetting
                    # this counter as new text arrives.
                    if (
                        stable >= 6
                        and not arena_busy
                    ):
                        try:
                            (
                                DEBUG_ROOT
                                / "arena-final-response.txt"
                            ).write_text(
                                answer,
                                encoding="utf-8",
                            )
                        except Exception:
                            pass

                        # =============================================
                        # TURNTOAPI_ARENA_DOM_PROBE_V1
                        #
                        # Inspect only visible DOM elements related to:
                        #
                        #   - Arena's "Thought for ..." region
                        #   - the final generated answer
                        #
                        # This lets us stop scraping document.body and
                        # identify the real response containers.
                        # =============================================

                        try:

                            answer_lines = [
                                line.strip()
                                for line in answer.splitlines()
                                if line.strip()
                            ]

                            probe_target = (
                                answer_lines[-1]
                                if answer_lines
                                else ""
                            )

                            dom_probe = driver.execute_script(
                                """
                                const target =
                                    String(arguments[0] || "").trim();

                                function visible(el) {

                                    if (!el) {
                                        return false;
                                    }

                                    const style =
                                        window.getComputedStyle(el);

                                    if (
                                        style.display === "none" ||
                                        style.visibility === "hidden"
                                    ) {
                                        return false;
                                    }

                                    const rect =
                                        el.getBoundingClientRect();

                                    return (
                                        rect.width > 0 &&
                                        rect.height > 0
                                    );
                                }

                                function attrs(el) {

                                    const result = {};

                                    for (
                                        const name of [
                                            "id",
                                            "class",
                                            "role",
                                            "aria-label",
                                            "data-testid",
                                            "data-state",
                                            "data-slot"
                                        ]
                                    ) {

                                        const value =
                                            el.getAttribute(name);

                                        if (value) {
                                            result[name] = value;
                                        }
                                    }

                                    return result;
                                }

                                function compactText(el) {

                                    return String(
                                        el.innerText || ""
                                    )
                                    .replace(
                                        /\\r/g,
                                        ""
                                    )
                                    .trim();
                                }

                                const nodes = Array.from(
                                    document.querySelectorAll(
                                        "body *"
                                    )
                                );

                                const hits = [];

                                for (const el of nodes) {

                                    if (!visible(el)) {
                                        continue;
                                    }

                                    const tag =
                                        String(
                                            el.tagName || ""
                                        ).toLowerCase();

                                    if (
                                        tag === "script" ||
                                        tag === "style" ||
                                        tag === "svg" ||
                                        tag === "path"
                                    ) {
                                        continue;
                                    }

                                    const text =
                                        compactText(el);

                                    if (
                                        !text ||
                                        text.length > 30000
                                    ) {
                                        continue;
                                    }

                                    const lower =
                                        text.toLowerCase();

                                    const thoughtHit =
                                        lower.includes(
                                            "thought for"
                                        );

                                    const targetHit =
                                        target &&
                                        text.includes(
                                            target
                                        );

                                    if (
                                        !thoughtHit &&
                                        !targetHit
                                    ) {
                                        continue;
                                    }

                                    let parent = el;
                                    const ancestors = [];

                                    for (
                                        let depth = 0;
                                        depth < 5 &&
                                        parent;
                                        depth++
                                    ) {

                                        ancestors.push({
                                            tag:
                                                String(
                                                    parent.tagName ||
                                                    ""
                                                ).toLowerCase(),

                                            attrs:
                                                attrs(parent),

                                            textLength:
                                                compactText(
                                                    parent
                                                ).length
                                        });

                                        parent =
                                            parent.parentElement;
                                    }

                                    hits.push({
                                        tag: tag,
                                        attrs: attrs(el),
                                        thoughtHit:
                                            thoughtHit,
                                        targetHit:
                                            targetHit,
                                        textLength:
                                            text.length,

                                        text:
                                            text.slice(
                                                0,
                                                6000
                                            ),

                                        html:
                                            String(
                                                el.outerHTML || ""
                                            ).slice(
                                                0,
                                                8000
                                            ),

                                        ancestors:
                                            ancestors
                                    });
                                }

                                hits.sort(
                                    (a, b) =>
                                        a.textLength -
                                        b.textLength
                                );

                                return {
                                    url:
                                        window.location.href,

                                    title:
                                        document.title,

                                    target:
                                        target,

                                    count:
                                        hits.length,

                                    hits:
                                        hits.slice(
                                            0,
                                            40
                                        )
                                };
                                """,
                                probe_target,
                            )

                            import json

                            (
                                DEBUG_ROOT
                                / "arena-dom-probe.json"
                            ).write_text(
                                json.dumps(
                                    dom_probe,
                                    indent=2,
                                    ensure_ascii=False,
                                ),
                                encoding="utf-8",
                            )

                        except Exception as exc:

                            try:
                                (
                                    DEBUG_ROOT
                                    / "arena-dom-probe-error.txt"
                                ).write_text(
                                    repr(exc),
                                    encoding="utf-8",
                                )
                            except Exception:
                                pass


                        return answer

                time.sleep(0.5)

            if answer:
                return answer

            try:
                (
                    DEBUG_ROOT
                    / "arena-timeout-body.txt"
                ).write_text(
                    page_text(),
                    encoding="utf-8",
                )

                (
                    DEBUG_ROOT
                    / "arena-current-url.txt"
                ).write_text(
                    driver.current_url,
                    encoding="utf-8",
                )

                driver.save_screenshot(
                    str(
                        DEBUG_ROOT
                        / "arena-headless-timeout.png"
                    )
                )

            except Exception:
                pass

            driver.save_screenshot(
                str(
                    DEBUG_ROOT
                    / "arena-no-response.png"
                )
            )

            raise RuntimeError(
                "Arena response was not detected."
            )

        finally:
            self._close_driver(driver)

    def _click_text(
        self,
        driver,
        wanted,
    ):
        target = _normalise(
            wanted
        )

        selector = (
            "button,"
            '[role="button"],'
            '[role="option"],'
            '[role="menuitem"]'
        )

        for _ in range(3):

            elements = _fresh_elements(
                driver,
                selector,
            )

            for element in elements:

                try:
                    text = _safe_text(
                        element
                    )

                    normal = _normalise(
                        text
                    )

                    if (
                        normal == target
                        or target in normal
                    ):
                        element.click()
                        time.sleep(0.5)
                        return True

                except StaleElementReferenceException:
                    break

        return False

    def _select_arena_model(
        self,
        driver,
        requested_model: str,
    ):
        """
        Select a specific model through Arena's current @ picker.

        Handles IDs such as:

            gemini-4-argon-high
            claude-fable-5-high
            claude-opus-5-max

        Arena's visible names may instead be:

            Gemini 4 Argon (High)
            Claude Fable 5 (High)
            Claude Opus 5 (Max)
        """

        # --------------------------------------------------------
        # Convert TurnToAPI slug into a human Arena search term.
        # --------------------------------------------------------

        raw = (
            requested_model
            .replace("_", " ")
            .replace("-", " ")
            .strip()
        )

        words = raw.split()

        level = None

        if words:
            last = words[-1].lower()

            if last in {
                "high",
                "max",
                "xhigh",
                "medium",
                "low",
            }:
                level = words.pop()

        base_name = " ".join(words)

        # Searching the base name is more reliable because Arena
        # may render High as "(High)" instead of "-high".
        search_query = base_name or raw

        # --------------------------------------------------------
        # Open @ selector by typing directly into Arena composer.
        # --------------------------------------------------------

        if not _fill_fresh(
            driver,
            ARENA_COMPOSERS,
            "@",
            timeout=7,
        ):
            raise RuntimeError(
                "Could not focus Arena composer "
                "to open @ model picker."
            )

        time.sleep(0.8)

        composer = _fresh_element(
            driver,
            ARENA_COMPOSERS,
            timeout=3,
        )

        if not composer:
            raise RuntimeError(
                "Arena composer disappeared after typing @."
            )

        # Continue typing after @ rather than clearing it.
        for ch in search_query:
            try:
                composer = _fresh_element(
                    driver,
                    ARENA_COMPOSERS,
                    timeout=2,
                )

                composer.send_keys(ch)

            except StaleElementReferenceException:
                composer = _fresh_element(
                    driver,
                    ARENA_COMPOSERS,
                    timeout=2,
                )

                if not composer:
                    raise

                composer.send_keys(ch)

            time.sleep(0.025)

        time.sleep(1.5)

        # --------------------------------------------------------
        # Debug: save visible page text after @ search.
        # --------------------------------------------------------

        try:
            body_text = driver.find_element(
                By.TAG_NAME,
                "body",
            ).text

            (
                DEBUG_ROOT
                / "arena-after-at.txt"
            ).write_text(
                body_text,
                encoding="utf-8",
            )

        except Exception:
            pass

        # --------------------------------------------------------
        # Search INSIDE likely popup/popover structures.
        #
        # Arena's current selector items do not necessarily expose
        # role=option, which caused the previous matcher to fail.
        # --------------------------------------------------------

        requested_tokens = list(
            _tokens(
                requested_model
                .replace("-", " ")
                .replace("_", " ")
            )
        )

        base_tokens = list(
            _tokens(base_name)
        )

        level_normal = (
            _normalise(level)
            if level
            else ""
        )

        result = driver.execute_script(
            r"""
            const requestedTokens = arguments[0];
            const baseTokens = arguments[1];
            const requestedLevel = arguments[2];

            function visible(el) {
                if (!el) return false;

                const s = getComputedStyle(el);

                if (
                    s.display === "none" ||
                    s.visibility === "hidden" ||
                    Number(s.opacity) === 0
                ) return false;

                const r = el.getBoundingClientRect();

                return (
                    r.width > 1 &&
                    r.height > 1
                );
            }

            function norm(s) {
                return String(s || "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, "");
            }

            function tokens(s) {
                return (
                    String(s || "")
                    .toLowerCase()
                    .match(/[a-z0-9]+/g)
                    || []
                );
            }

            // Prioritise elements that are likely to belong to a
            // dropdown, popover, command palette, or list.
            const roots = [
                ...document.querySelectorAll(
                    '[role="listbox"],' +
                    '[role="dialog"],' +
                    '[role="menu"],' +
                    '[data-radix-popper-content-wrapper],' +
                    '[data-slot*="popover"],' +
                    '[data-slot*="command"],' +
                    '[cmdk-list],' +
                    '[class*="popover"],' +
                    '[class*="dropdown"],' +
                    '[class*="command"]'
                )
            ].filter(visible);

            const scopes =
                roots.length > 0
                ? roots
                : [document.body];

            let entries = [];

            for (const root of scopes) {
                for (const el of root.querySelectorAll("*")) {

                    if (!visible(el)) continue;

                    const text =
                        (el.innerText || "")
                        .trim();

                    if (!text) continue;

                    // Avoid giant containers.
                    if (text.length > 180) continue;

                    const ts = tokens(text);

                    let overlap = 0;

                    for (const t of baseTokens) {
                        if (ts.includes(t)) {
                            overlap++;
                        }
                    }

                    if (!overlap) continue;

                    let score =
                        overlap /
                        Math.max(
                            baseTokens.length,
                            1
                        );

                    const n = norm(text);

                    if (
                        requestedLevel &&
                        n.includes(requestedLevel)
                    ) {
                        score += 0.3;
                    }

                    // Prefer relatively small leaf-ish elements.
                    if (el.children.length <= 3) {
                        score += 0.15;
                    }

                    // Prefer interactive-looking elements.
                    const role =
                        el.getAttribute("role") || "";

                    if (
                        el.tagName === "BUTTON" ||
                        role === "option" ||
                        role === "menuitem" ||
                        role === "button" ||
                        el.hasAttribute("data-slot") ||
                        el.hasAttribute("cmdk-item")
                    ) {
                        score += 0.2;
                    }

                    entries.push({
                        el,
                        text,
                        score
                    });
                }
            }

            entries.sort(
                (a, b) => b.score - a.score
            );

            if (!entries.length) {
                return {
                    clicked: false,
                    reason: "no-candidates",
                    candidates: []
                };
            }

            const best = entries[0];

            if (best.score < 0.65) {
                return {
                    clicked: false,
                    reason: "low-score",
                    candidates:
                        entries
                        .slice(0, 15)
                        .map(x => x.text)
                };
            }

            let clickTarget = best.el;

            // Walk upwards looking for the actual interactive row.
            let parent = best.el;

            for (let i = 0; i < 5 && parent; i++) {

                const role =
                    parent.getAttribute
                    ? parent.getAttribute("role")
                    : "";

                if (
                    parent.tagName === "BUTTON" ||
                    role === "option" ||
                    role === "menuitem" ||
                    role === "button" ||
                    (
                        parent.hasAttribute &&
                        (
                            parent.hasAttribute("cmdk-item") ||
                            (
                                parent.getAttribute("data-slot")
                                || ""
                            ).includes("command")
                        )
                    )
                ) {
                    clickTarget = parent;
                    break;
                }

                parent = parent.parentElement;
            }

            clickTarget.scrollIntoView({
                block: "center"
            });

            clickTarget.click();

            return {
                clicked: true,
                selected: best.text,
                score: best.score,
                candidates:
                    entries
                    .slice(0, 15)
                    .map(x => x.text)
            };
            """,
            requested_tokens,
            base_tokens,
            level_normal,
        )

        candidates = []

        if isinstance(result, dict):
            candidates = (
                result.get("candidates")
                or []
            )

        (
            DEBUG_ROOT
            / "arena-visible-models.txt"
        ).write_text(
            "\n".join(
                dict.fromkeys(candidates)
            ),
            encoding="utf-8",
        )

        if (
            isinstance(result, dict)
            and result.get("clicked")
        ):
            time.sleep(1)

            (
                DEBUG_ROOT
                / "arena-selected-model.txt"
            ).write_text(
                str(
                    result.get(
                        "selected",
                        requested_model,
                    )
                ),
                encoding="utf-8",
            )

            return

        # --------------------------------------------------------
        # Keyboard fallback.
        #
        # If the popup is present but uses virtualized/nonstandard
        # elements, full search text should place the intended model
        # first. ArrowDown + Enter then chooses it.
        # --------------------------------------------------------

        composer = _fresh_element(
            driver,
            ARENA_COMPOSERS,
            timeout=3,
        )

        if composer:

            try:
                composer.send_keys(
                    Keys.ARROW_DOWN
                )

                time.sleep(0.3)

                composer.send_keys(
                    Keys.ENTER
                )

                time.sleep(1)

                # If the @ query disappeared/replaced itself with a
                # mention chip, assume the selection succeeded.
                try:
                    value = (
                        composer.get_attribute(
                            "value"
                        )
                        or composer.text
                        or ""
                    )

                except StaleElementReferenceException:
                    value = ""

                if (
                    not value
                    or not value
                    .lower()
                    .startswith("@")
                ):
                    (
                        DEBUG_ROOT
                        / "arena-selected-model.txt"
                    ).write_text(
                        "Keyboard fallback selected "
                        + requested_model,
                        encoding="utf-8",
                    )

                    return

            except Exception:
                pass

        driver.save_screenshot(
            str(
                DEBUG_ROOT
                / "arena-model-not-found.png"
            )
        )

        raise RuntimeError(
            f"Arena @ selector opened but "
            f"'{requested_model}' could not be selected. "
            "See arena-after-at.txt and "
            "arena-visible-models.txt."
        )
