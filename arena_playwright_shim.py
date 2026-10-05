from __future__ import annotations

import atexit
import inspect
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path


from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)


ROOT = Path(__file__).resolve().parent

PROFILE = (
    ROOT
    / "playwright_profiles"
    / "arena"
)

DEBUG = (
    ROOT
    / "browser_debug"
)

DEBUG.mkdir(
    parents=True,
    exist_ok=True,
)

PROFILE.mkdir(
    parents=True,
    exist_ok=True,
)


# ================================================================
# SELECTORS
# ================================================================

def _quote_css(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def _xpath_literal(value):
    value = str(value)

    if "'" not in value:
        return "'" + value + "'"

    if '"' not in value:
        return '"' + value + '"'

    pieces = value.split("'")

    parts = []

    for index, piece in enumerate(pieces):

        if piece:
            parts.append(
                "'" + piece + "'"
            )

        if index < len(pieces) - 1:
            parts.append(
                '"\'"'
            )

    return (
        "concat("
        + ",".join(parts)
        + ")"
    )


def _selector(
    by,
    value,
    relative=False,
):
    by = str(
        by or ""
    ).lower()

    value = str(
        value or ""
    )

    if by in (
        "css selector",
        "css",
    ):
        return value

    if by == "id":
        return (
            '[id="'
            + _quote_css(value)
            + '"]'
        )

    if by == "name":
        return (
            '[name="'
            + _quote_css(value)
            + '"]'
        )

    if by == "class name":
        return (
            '[class~="'
            + _quote_css(value)
            + '"]'
        )

    if by == "tag name":
        return value

    if by == "xpath":

        if (
            relative
            and value.startswith("//")
        ):
            value = "." + value

        return "xpath=" + value

    if by == "link text":

        prefix = (
            ".//"
            if relative
            else "//"
        )

        return (
            "xpath="
            + prefix
            + "a[normalize-space(.)="
            + _xpath_literal(value)
            + "]"
        )

    if by == "partial link text":

        prefix = (
            ".//"
            if relative
            else "//"
        )

        return (
            "xpath="
            + prefix
            + "a[contains(normalize-space(.),"
            + _xpath_literal(value)
            + ")]"
        )

    return value


# ================================================================
# PLAYWRIGHT ENGINE
#
# Playwright objects stay on ONE dedicated thread.
#
# This matters because TurnToAPI's live DOM watcher reads the driver
# from a secondary Python thread.
# ================================================================

class _Engine:

    def __init__(
        self,
    ):

        self.commands = queue.Queue()

        self.ready = threading.Event()

        self.start_error = None

        self.thread = threading.Thread(
            target=self._run,
            name="TurnToAPI-Playwright-Arena",
            daemon=True,
        )

        self.thread.start()

        if not self.ready.wait(
            90
        ):
            raise RuntimeError(
                "Timed out starting persistent Playwright Firefox."
            )

        if self.start_error:
            raise RuntimeError(
                "Could not start persistent Playwright Firefox: "
                + repr(
                    self.start_error
                )
            )


    def _launch(
        self,
    ):

        from playwright.sync_api import (
            sync_playwright,
        )

        self.pw = (
            sync_playwright()
            .start()
        )

        self.context = (
            self.pw.firefox
            .launch_persistent_context(
                user_data_dir=str(
                    PROFILE
                ),
                headless=False,
                viewport={
                    "width": 1440,
                    "height": 1000,
                },
                firefox_user_prefs={
                    "dom.webnotifications.enabled":
                        False,

                    "browser.shell.checkDefaultBrowser":
                        False,
                },
            )
        )

        self.context.set_default_timeout(
            15000
        )

        self.context.set_default_navigation_timeout(
            90000
        )

        pages = (
            self.context.pages
        )

        if pages:

            self.page = pages[0]

        else:

            self.page = (
                self.context.new_page()
            )

        self.page_load_timeout = 90000

        self.script_timeout = 90000

        try:

            (
                DEBUG
                / "arena-playwright-runtime.txt"
            ).write_text(
                (
                    "Persistent Arena Playwright Firefox started.\n"
                    + "Profile: "
                    + str(PROFILE)
                    + "\n"
                ),
                encoding="utf-8",
            )

        except Exception:
            pass


    def _shutdown(
        self,
    ):

        try:

            if getattr(
                self,
                "context",
                None,
            ):

                self.context.close()

        except Exception:
            pass

        try:

            if getattr(
                self,
                "pw",
                None,
            ):

                self.pw.stop()

        except Exception:
            pass


    def _run(
        self,
    ):

        try:

            self._launch()

        except Exception as exc:

            self.start_error = exc

            self.ready.set()

            return


        self.ready.set()


        while True:

            item = (
                self.commands.get()
            )

            if item is None:

                self._shutdown()

                return


            (
                operation,
                args,
                kwargs,
                response,
            ) = item


            try:

                result = self._dispatch(
                    operation,
                    *args,
                    **kwargs,
                )

                response.put(
                    (
                        True,
                        result,
                    )
                )


            except Exception as exc:

                response.put(
                    (
                        False,
                        exc,
                    )
                )


    def call(
        self,
        operation,
        *args,
        **kwargs,
    ):

        response = queue.Queue(
            maxsize=1
        )

        self.commands.put(
            (
                operation,
                args,
                kwargs,
                response,
            )
        )

        try:

            ok, value = response.get(
                timeout=300
            )

        except queue.Empty:

            raise TimeoutException(
                "Timed out waiting for Playwright browser thread."
            )


        if ok:
            return value

        raise value


    def _locator(
        self,
        descriptor,
    ):

        locator = None

        for part in descriptor:

            selector = (
                part[
                    "selector"
                ]
            )

            index = int(
                part.get(
                    "index",
                    0,
                )
            )


            if locator is None:

                locator = (
                    self.page.locator(
                        selector
                    )
                )

            else:

                locator = (
                    locator.locator(
                        selector
                    )
                )


            locator = (
                locator.nth(
                    index
                )
            )


        if locator is None:

            locator = (
                self.page.locator(
                    "html"
                )
            )

        return locator


    def _convert_argument(
        self,
        value,
    ):

        if isinstance(
            value,
            dict,
        ):

            if (
                "__turntoapi_element__"
                in value
            ):

                locator = self._locator(
                    value[
                        "__turntoapi_element__"
                    ]
                )

                return (
                    locator.element_handle(
                        timeout=15000
                    )
                )


            return {
                key:
                    self._convert_argument(
                        item
                    )

                for key, item
                in value.items()
            }


        if isinstance(
            value,
            list,
        ):

            return [
                self._convert_argument(
                    item
                )

                for item
                in value
            ]


        if isinstance(
            value,
            tuple,
        ):

            return [
                self._convert_argument(
                    item
                )

                for item
                in value
            ]


        return value


    def _dispatch(
        self,
        operation,
        *args,
        **kwargs,
    ):

        if operation == "goto":

            url = args[0]

            try:

                self.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.page_load_timeout,
                )

            except Exception as exc:

                text = str(
                    exc
                ).lower()

                if (
                    "timeout"
                    not in text
                ):
                    raise

            return None


        if operation == "current_url":

            return (
                self.page.url
            )


        if operation == "title":

            return (
                self.page.title()
            )


        if operation == "page_source":

            return (
                self.page.content()
            )


        if operation == "refresh":

            self.page.reload(
                wait_until="domcontentloaded",
                timeout=self.page_load_timeout,
            )

            return None


        if operation == "back":

            self.page.go_back(
                wait_until="domcontentloaded",
                timeout=self.page_load_timeout,
            )

            return None


        if operation == "forward":

            self.page.go_forward(
                wait_until="domcontentloaded",
                timeout=self.page_load_timeout,
            )

            return None


        if operation == "set_page_load_timeout":

            self.page_load_timeout = int(
                float(
                    args[0]
                )
                * 1000
            )

            return None


        if operation == "set_script_timeout":

            self.script_timeout = int(
                float(
                    args[0]
                )
                * 1000
            )

            return None


        if operation == "implicitly_wait":

            self.context.set_default_timeout(
                int(
                    float(
                        args[0]
                    )
                    * 1000
                )
            )

            return None


        if operation == "eval":

            script = str(
                args[0]
            )

            original_args = (
                args[1]
                if len(args) > 1
                else []
            )

            converted = (
                self._convert_argument(
                    original_args
                )
            )

            wrapper = (
                "(args) => {"
                "return (function(){"
                + script
                + "}).apply(null,args);"
                "}"
            )

            return (
                self.page.evaluate(
                    wrapper,
                    converted,
                )
            )


        if operation == "count":

            descriptor = args[0]

            selector = args[1]

            if descriptor:

                base = (
                    self._locator(
                        descriptor
                    )
                )

                locator = (
                    base.locator(
                        selector
                    )
                )

            else:

                locator = (
                    self.page.locator(
                        selector
                    )
                )

            return (
                locator.count()
            )


        if operation == "text":

            locator = (
                self._locator(
                    args[0]
                )
            )

            return (
                locator.inner_text(
                    timeout=15000
                )
            )


        if operation == "click":

            locator = (
                self._locator(
                    args[0]
                )
            )

            locator.click(
                timeout=15000
            )

            return None


        if operation == "clear":

            locator = (
                self._locator(
                    args[0]
                )
            )

            try:

                locator.fill(
                    "",
                    timeout=15000,
                )

            except Exception:

                locator.press(
                    "Control+A"
                )

                locator.press(
                    "Delete"
                )

            return None


        if operation == "type":

            locator = (
                self._locator(
                    args[0]
                )
            )

            value = str(
                args[1]
            )

            locator.type(
                value,
                delay=0,
                timeout=15000,
            )

            return None


        if operation == "press":

            locator = (
                self._locator(
                    args[0]
                )
            )

            locator.press(
                str(
                    args[1]
                ),
                timeout=15000,
            )

            return None


        if operation == "attribute":

            locator = (
                self._locator(
                    args[0]
                )
            )

            name = str(
                args[1]
            )


            if name == "outerHTML":

                return (
                    locator.evaluate(
                        "(el) => el.outerHTML"
                    )
                )


            if name == "innerHTML":

                return (
                    locator.inner_html()
                )


            if name == "textContent":

                return (
                    locator.text_content()
                )


            if name == "value":

                try:

                    return (
                        locator.input_value()
                    )

                except Exception:
                    pass


            return (
                locator.get_attribute(
                    name
                )
            )


        if operation == "property":

            locator = (
                self._locator(
                    args[0]
                )
            )

            name = str(
                args[1]
            )

            return (
                locator.evaluate(
                    "(el,name) => el[name]",
                    name,
                )
            )


        if operation == "displayed":

            return (
                self._locator(
                    args[0]
                )
                .is_visible()
            )


        if operation == "enabled":

            return (
                self._locator(
                    args[0]
                )
                .is_enabled()
            )


        if operation == "selected":

            locator = (
                self._locator(
                    args[0]
                )
            )

            try:

                return (
                    locator.is_checked()
                )

            except Exception:

                return (
                    str(
                        locator.get_attribute(
                            "aria-selected"
                        )
                        or ""
                    )
                    .lower()
                    == "true"
                )


        if operation == "rect":

            box = (
                self._locator(
                    args[0]
                )
                .bounding_box()
            )

            if not box:

                return {
                    "x": 0,
                    "y": 0,
                    "width": 0,
                    "height": 0,
                }

            return {
                "x":
                    box.get(
                        "x",
                        0,
                    ),

                "y":
                    box.get(
                        "y",
                        0,
                    ),

                "width":
                    box.get(
                        "width",
                        0,
                    ),

                "height":
                    box.get(
                        "height",
                        0,
                    ),
            }


        if operation == "tag_name":

            return (
                self._locator(
                    args[0]
                )
                .evaluate(
                    "(el) => el.tagName.toLowerCase()"
                )
            )


        if operation == "css":

            locator = (
                self._locator(
                    args[0]
                )
            )

            name = str(
                args[1]
            )

            return (
                locator.evaluate(
                    (
                        "(el,name) => "
                        "getComputedStyle(el)"
                        ".getPropertyValue(name)"
                    ),
                    name,
                )
            )


        if operation == "screenshot":

            self.page.screenshot(
                path=str(
                    args[0]
                ),
                full_page=True,
            )

            return True


        if operation == "element_screenshot":

            self._locator(
                args[0]
            ).screenshot(
                path=str(
                    args[1]
                )
            )

            return True


        if operation == "scroll":

            self._locator(
                args[0]
            ).scroll_into_view_if_needed()

            return None


        if operation == "window_size":

            size = (
                self.page.viewport_size
                or {
                    "width": 1440,
                    "height": 1000,
                }
            )

            return {
                "width":
                    size.get(
                        "width",
                        1440,
                    ),

                "height":
                    size.get(
                        "height",
                        1000,
                    ),
            }


        if operation == "set_window_size":

            self.page.set_viewport_size({
                "width":
                    int(
                        args[0]
                    ),

                "height":
                    int(
                        args[1]
                    ),
            })

            return None


        if operation == "pages":

            return list(
                range(
                    len(
                        self.context.pages
                    )
                )
            )


        if operation == "switch_page":

            index = int(
                args[0]
            )

            pages = (
                self.context.pages
            )

            if (
                index < 0
                or index >= len(
                    pages
                )
            ):
                raise WebDriverException(
                    "Invalid window handle."
                )

            self.page = (
                pages[
                    index
                ]
            )

            return None


        raise WebDriverException(
            "Unsupported Playwright shim operation: "
            + str(
                operation
            )
        )


_ENGINE = None

_ENGINE_LOCK = threading.Lock()


def _engine():

    global _ENGINE

    with _ENGINE_LOCK:

        if _ENGINE is None:

            _ENGINE = (
                _Engine()
            )

        return _ENGINE


def _shutdown_engine():

    global _ENGINE

    with _ENGINE_LOCK:

        engine = _ENGINE

        _ENGINE = None


    if engine:

        try:

            engine.commands.put(
                None
            )

        except Exception:
            pass


atexit.register(
    _shutdown_engine
)


# ================================================================
# WEB ELEMENT COMPATIBILITY
# ================================================================

_SPECIAL = {
    "\ue003": "Backspace",
    "\ue004": "Tab",
    "\ue006": "Enter",
    "\ue007": "Enter",
    "\ue00c": "Escape",
    "\ue00e": "PageUp",
    "\ue00f": "PageDown",
    "\ue010": "End",
    "\ue011": "Home",
    "\ue012": "ArrowLeft",
    "\ue013": "ArrowUp",
    "\ue014": "ArrowRight",
    "\ue015": "ArrowDown",
    "\ue017": "Delete",
}

_MODIFIERS = {
    "\ue008": "Shift",
    "\ue009": "Control",
    "\ue00a": "Alt",
    "\ue03d": "Meta",
}


class PlaywrightWebElement:

    def __init__(
        self,
        driver,
        descriptor,
    ):

        self._driver = driver

        self._descriptor = (
            list(
                descriptor
            )
        )

        self.id = (
            "pw-"
            + str(
                abs(
                    hash(
                        json.dumps(
                            self._descriptor,
                            sort_keys=True,
                        )
                    )
                )
            )
        )


    def _argument(
        self,
    ):

        return {
            "__turntoapi_element__":
                self._descriptor
        }


    @property
    def text(
        self,
    ):

        return self._driver._engine.call(
            "text",
            self._descriptor,
        )


    @property
    def tag_name(
        self,
    ):

        return self._driver._engine.call(
            "tag_name",
            self._descriptor,
        )


    @property
    def rect(
        self,
    ):

        return self._driver._engine.call(
            "rect",
            self._descriptor,
        )


    @property
    def size(
        self,
    ):

        rect = self.rect

        return {
            "width":
                rect.get(
                    "width",
                    0,
                ),

            "height":
                rect.get(
                    "height",
                    0,
                ),
        }


    @property
    def location(
        self,
    ):

        rect = self.rect

        return {
            "x":
                rect.get(
                    "x",
                    0,
                ),

            "y":
                rect.get(
                    "y",
                    0,
                ),
        }


    @property
    def location_once_scrolled_into_view(
        self,
    ):

        self._driver._engine.call(
            "scroll",
            self._descriptor,
        )

        return self.location


    def click(
        self,
    ):

        return self._driver._engine.call(
            "click",
            self._descriptor,
        )


    def clear(
        self,
    ):

        return self._driver._engine.call(
            "clear",
            self._descriptor,
        )


    def send_keys(
        self,
        *values,
    ):

        text = "".join(
            str(
                value
            )

            for value
            in values
        )

        normal = ""

        modifiers = []


        def flush():

            nonlocal normal

            if normal:

                self._driver._engine.call(
                    "type",
                    self._descriptor,
                    normal,
                )

                normal = ""


        for char in text:

            if char in _MODIFIERS:

                flush()

                modifiers.append(
                    _MODIFIERS[
                        char
                    ]
                )

                continue


            if char in _SPECIAL:

                flush()

                key = (
                    _SPECIAL[
                        char
                    ]
                )

                if modifiers:

                    key = (
                        "+".join(
                            modifiers
                            + [
                                key
                            ]
                        )
                    )

                    modifiers = []


                self._driver._engine.call(
                    "press",
                    self._descriptor,
                    key,
                )

                continue


            if modifiers:

                flush()

                key = (
                    "+".join(
                        modifiers
                        + [
                            char.upper()
                        ]
                    )
                )

                modifiers = []

                self._driver._engine.call(
                    "press",
                    self._descriptor,
                    key,
                )

                continue


            normal += char


        flush()


    def get_attribute(
        self,
        name,
    ):

        return self._driver._engine.call(
            "attribute",
            self._descriptor,
            name,
        )


    def get_dom_attribute(
        self,
        name,
    ):

        return self.get_attribute(
            name
        )


    def get_property(
        self,
        name,
    ):

        return self._driver._engine.call(
            "property",
            self._descriptor,
            name,
        )


    def value_of_css_property(
        self,
        name,
    ):

        return self._driver._engine.call(
            "css",
            self._descriptor,
            name,
        )


    def is_displayed(
        self,
    ):

        return bool(
            self._driver._engine.call(
                "displayed",
                self._descriptor,
            )
        )


    def is_enabled(
        self,
    ):

        return bool(
            self._driver._engine.call(
                "enabled",
                self._descriptor,
            )
        )


    def is_selected(
        self,
    ):

        return bool(
            self._driver._engine.call(
                "selected",
                self._descriptor,
            )
        )


    def find_elements(
        self,
        by="id",
        value=None,
    ):

        selector = _selector(
            by,
            value,
            relative=True,
        )

        count = (
            self._driver._engine.call(
                "count",
                self._descriptor,
                selector,
            )
        )

        return [
            PlaywrightWebElement(
                self._driver,
                (
                    self._descriptor
                    + [
                        {
                            "selector":
                                selector,

                            "index":
                                index,
                        }
                    ]
                ),
            )

            for index
            in range(
                count
            )
        ]


    def find_element(
        self,
        by="id",
        value=None,
    ):

        found = self.find_elements(
            by,
            value,
        )

        if not found:

            raise NoSuchElementException(
                "Element not found: "
                + str(
                    value
                )
            )

        return found[0]


    def screenshot(
        self,
        filename,
    ):

        return self._driver._engine.call(
            "element_screenshot",
            self._descriptor,
            filename,
        )


    def submit(
        self,
    ):

        self._driver._engine.call(
            "press",
            self._descriptor,
            "Enter",
        )


# ================================================================
# SWITCH-TO COMPATIBILITY
# ================================================================

class _SwitchTo:

    def __init__(
        self,
        driver,
    ):

        self.driver = driver


    @property
    def active_element(
        self,
    ):

        return PlaywrightWebElement(
            self.driver,
            [
                {
                    "selector":
                        ":focus",

                    "index":
                        0,
                }
            ],
        )


    def default_content(
        self,
    ):

        return None


    def parent_frame(
        self,
    ):

        return None


    def frame(
        self,
        frame_reference,
    ):

        return None


    def window(
        self,
        handle,
    ):

        try:

            index = int(
                str(
                    handle
                ).replace(
                    "pw-window-",
                    "",
                )
            )

        except Exception:

            raise WebDriverException(
                "Invalid window handle."
            )

        self.driver._engine.call(
            "switch_page",
            index,
        )


# ================================================================
# SELENIUM-COMPATIBLE DRIVER
# ================================================================

class PlaywrightFirefoxDriver:

    def __init__(
        self,
    ):

        self._engine = (
            _engine()
        )

        self.session_id = (
            "playwright-"
            + uuid.uuid4().hex
        )

        self.name = "firefox"

        self.capabilities = {
            "browserName":
                "firefox",

            "turntoapi":
                "playwright-persistent",
        }

        self.switch_to = (
            _SwitchTo(
                self
            )
        )

        class _Service:
            process = None

        self.service = (
            _Service()
        )


    def get(
        self,
        url,
    ):

        return self._engine.call(
            "goto",
            str(
                url
            ),
        )


    @property
    def current_url(
        self,
    ):

        return self._engine.call(
            "current_url"
        )


    @property
    def title(
        self,
    ):

        return self._engine.call(
            "title"
        )


    @property
    def page_source(
        self,
    ):

        return self._engine.call(
            "page_source"
        )


    def find_elements(
        self,
        by="id",
        value=None,
    ):

        selector = _selector(
            by,
            value,
            relative=False,
        )

        count = (
            self._engine.call(
                "count",
                [],
                selector,
            )
        )

        return [
            PlaywrightWebElement(
                self,
                [
                    {
                        "selector":
                            selector,

                        "index":
                            index,
                    }
                ],
            )

            for index
            in range(
                count
            )
        ]


    def find_element(
        self,
        by="id",
        value=None,
    ):

        found = self.find_elements(
            by,
            value,
        )

        if not found:

            raise NoSuchElementException(
                "Element not found: "
                + str(
                    value
                )
            )

        return found[0]


    def execute_script(
        self,
        script,
        *args,
    ):

        converted = []

        for item in args:

            if isinstance(
                item,
                PlaywrightWebElement,
            ):

                converted.append(
                    item._argument()
                )

            else:

                converted.append(
                    item
                )


        return self._engine.call(
            "eval",
            str(
                script
            ),
            converted,
        )


    def execute_async_script(
        self,
        script,
        *args,
    ):

        # Current Arena adapter does not depend on Selenium's callback
        # form. Keep compatibility for scripts that return/promises.
        return self.execute_script(
            script,
            *args,
        )


    def refresh(
        self,
    ):

        return self._engine.call(
            "refresh"
        )


    def back(
        self,
    ):

        return self._engine.call(
            "back"
        )


    def forward(
        self,
    ):

        return self._engine.call(
            "forward"
        )


    def set_page_load_timeout(
        self,
        seconds,
    ):

        return self._engine.call(
            "set_page_load_timeout",
            seconds,
        )


    def set_script_timeout(
        self,
        seconds,
    ):

        return self._engine.call(
            "set_script_timeout",
            seconds,
        )


    def implicitly_wait(
        self,
        seconds,
    ):

        return self._engine.call(
            "implicitly_wait",
            seconds,
        )


    def maximize_window(
        self,
    ):

        try:

            self._engine.call(
                "set_window_size",
                1600,
                1000,
            )

        except Exception:
            pass


    def minimize_window(
        self,
    ):

        # Keep the persistent browser alive.
        return None


    def fullscreen_window(
        self,
    ):

        return self.maximize_window()


    def set_window_size(
        self,
        width,
        height,
        windowHandle="current",
    ):

        return self._engine.call(
            "set_window_size",
            width,
            height,
        )


    def get_window_size(
        self,
        windowHandle="current",
    ):

        return self._engine.call(
            "window_size"
        )


    @property
    def window_handles(
        self,
    ):

        pages = self._engine.call(
            "pages"
        )

        return [
            "pw-window-"
            + str(
                index
            )

            for index
            in pages
        ]


    @property
    def current_window_handle(
        self,
    ):

        return "pw-window-0"


    def save_screenshot(
        self,
        filename,
    ):

        return self._engine.call(
            "screenshot",
            filename,
        )


    def get_screenshot_as_file(
        self,
        filename,
    ):

        return self.save_screenshot(
            filename
        )


    def quit(
        self,
    ):

        #
        # INTENTIONALLY NO-OP.
        #
        # TurnToAPI may call driver.quit() after individual API calls.
        # Arena's Playwright context is intentionally persistent and is
        # closed only when the backend Python process exits.
        #

        return None


    def close(
        self,
    ):

        #
        # Also intentionally persistent.
        #

        return None


# ================================================================
# SINGLE PERSISTENT DRIVER
# ================================================================

_DRIVER = None

_DRIVER_LOCK = threading.Lock()


def _persistent_driver():

    global _DRIVER

    with _DRIVER_LOCK:

        if _DRIVER is None:

            _DRIVER = (
                PlaywrightFirefoxDriver()
            )

        return _DRIVER


# ================================================================
# ONLY INTERCEPT ARENA'S FIREFOX CREATION
# ================================================================

def _looks_like_arena_options(
    args,
    kwargs,
):

    pieces = []

    options = (
        kwargs.get(
            "options"
        )
    )

    if options is not None:

        try:

            pieces.extend(
                list(
                    options.arguments
                )
            )

        except Exception:
            pass


        try:

            pieces.append(
                json.dumps(
                    options.to_capabilities(),
                    default=str,
                )
            )

        except Exception:
            pass


    for item in args:

        try:
            pieces.append(
                str(
                    item
                )
            )

        except Exception:
            pass


    joined = " ".join(
        pieces
    ).lower()


    if (
        "firefox_profiles"
        in joined
        and
        "arena"
        in joined
    ):
        return True


    if (
        "firefox_runtime"
        in joined
        and
        "arena"
        in joined
    ):
        return True


    return False


def _called_from_arena():

    try:

        for frame in inspect.stack()[
            1:18
        ]:

            if (
                frame.function
                == "_arena"
            ):

                return True

    except Exception:
        pass


    return False


def _factory(
    original,
):

    def create(
        *args,
        **kwargs,
    ):

        if (
            _called_from_arena()
            or
            _looks_like_arena_options(
                args,
                kwargs,
            )
        ):

            return (
                _persistent_driver()
            )


        return original(
            *args,
            **kwargs,
        )


    create.__name__ = (
        getattr(
            original,
            "__name__",
            "Firefox",
        )
    )

    return create


def install_into_adapter(
    namespace,
):

    """
    Patch Firefox construction inside browser_web_adapter.py.

    ChatGPT / other Firefox paths continue using the original
    Selenium Firefox implementation.

    Arena receives one Playwright persistent context.
    """

    import selenium.webdriver as selenium_webdriver


    if not hasattr(
        selenium_webdriver,
        "_turntoapi_original_firefox",
    ):

        original = (
            selenium_webdriver.Firefox
        )

        selenium_webdriver._turntoapi_original_firefox = (
            original
        )

        selenium_webdriver.Firefox = (
            _factory(
                original
            )
        )


    patched = (
        selenium_webdriver.Firefox
    )


    webdriver_object = (
        namespace.get(
            "webdriver"
        )
    )


    if webdriver_object is not None:

        try:

            webdriver_object.Firefox = (
                patched
            )

        except Exception:
            pass


    local_firefox = (
        namespace.get(
            "Firefox"
        )
    )


    if (
        callable(
            local_firefox
        )
        and
        local_firefox
        is not patched
    ):

        namespace[
            "Firefox"
        ] = (
            _factory(
                local_firefox
            )
        )


    return True