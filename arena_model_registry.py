import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs


ROOT = Path(__file__).resolve().parent
LIVE_FILE = ROOT / "arena_models_live.json"
DEBUG_ROOT = ROOT / "browser_debug"
LOCK_FILE = ROOT / "arena_model_scan.lock"
STATE_FILE = DEBUG_ROOT / "arena-model-scan-last.json"

DEBUG_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)


IGNORE_LINES = {
    "",
    "max",
    "router",
    "direct",
    "battle",
    "auto",
    "text",
    "code",
    "image",
    "web",
    "new chat",
    "leaderboard",
    "search",
    "today",
    "yesterday",
    "download",
    "search models",
}


MODELISH = re.compile(
    r"^[A-Za-z0-9]"
    r"[A-Za-z0-9._+\-/]*"
    r"(?:\s+\([A-Za-z0-9._+\-/ ]+\))?$"
)


def _read_registry():
    try:
        return json.loads(
            LIVE_FILE.read_text(
                encoding="utf-8-sig"
            )
        )
    except Exception:
        return {
            "generated": 0,
            "source": "empty",
            "text": [],
            "code": [],
        }


def _write_registry(data):
    data["generated"] = int(
        time.time()
    )

    temp = LIVE_FILE.with_suffix(
        ".json.tmp"
    )

    temp.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temp.replace(
        LIVE_FILE
    )


def _entries(mode):
    data = _read_registry()

    values = data.get(
        mode,
        [],
    )

    result = []

    for item in values:

        if isinstance(
            item,
            str,
        ):
            result.append({
                "id": item,
                "label": item,
                "route": item,
                "verified": True,
            })

        elif isinstance(
            item,
            dict,
        ):
            model_id = str(
                item.get(
                    "id",
                    "",
                )
            ).strip()

            if not model_id:
                continue

            result.append({
                "id":
                    model_id,

                "label":
                    str(
                        item.get(
                            "label",
                            model_id,
                        )
                    ).strip()
                    or model_id,

                "route":
                    str(
                        item.get(
                            "route",
                            model_id,
                        )
                    ).strip()
                    or model_id,

                "verified":
                    bool(
                        item.get(
                            "verified",
                            False,
                        )
                    ),
            })

    return result


def get_entry(
    mode,
    model_id,
):
    model_id = str(
        model_id or ""
    ).strip()

    if not model_id:
        return None

    lower = model_id.lower()

    for entry in _entries(
        mode
    ):
        if (
            entry["id"].lower()
            == lower
        ):
            return entry

    return None


def known_model(
    mode,
    model_id,
):
    return (
        get_entry(
            mode,
            model_id,
        )
        is not None
    )


def was_verified(
    mode,
    model_id,
):
    entry = get_entry(
        mode,
        model_id,
    )

    return bool(
        entry
        and entry.get(
            "verified"
        )
    )


def get_route(
    mode,
    model_id,
):
    entry = get_entry(
        mode,
        model_id,
    )

    if not entry:
        return str(
            model_id or ""
        ).strip()

    return (
        entry.get(
            "route"
        )
        or entry["id"]
    )


def mark_working(
    mode,
    model_id,
    actual_route=None,
):
    data = _read_registry()

    changed = False

    for item in data.get(
        mode,
        []
    ):

        if isinstance(
            item,
            str,
        ):
            continue

        if (
            str(
                item.get(
                    "id",
                    ""
                )
            ).lower()
            !=
            str(
                model_id
            ).lower()
        ):
            continue

        item["verified"] = True

        if actual_route:
            item["route"] = str(
                actual_route
            )

        changed = True
        break

    if changed:
        data["source"] = (
            "runtime-route-verification"
        )

        _write_registry(
            data
        )


def _current_route(
    driver,
):
    try:
        parsed = urlparse(
            str(
                driver.current_url
                or ""
            )
        )

        return parse_qs(
            parsed.query
        ).get(
            "model_a",
            [""],
        )[0]

    except Exception:
        return ""


def _body_text(
    driver,
):
    try:
        return str(
            driver.execute_script(
                """
                return document.body
                    ? document.body.innerText
                    : "";
                """
            )
            or ""
        )
    except Exception:
        return ""


def _challenge_present(
    driver,
):
    try:
        title = str(
            driver.title
            or ""
        ).lower()
    except Exception:
        title = ""

    body = _body_text(
        driver
    ).lower()

    markers = (
        "cloudflare",
        "verify you are human",
        "verification successful",
        "performing security verification",
        "security verification",
        "protected by recaptcha",
        "recaptcha",
        "challenge-platform",
    )

    if (
        "just a moment" in title
    ):
        return True

    for marker in markers:
        if marker in body:
            return True

    return False


# TURNTOAPI_CLOUDFLARE_STREAK_V2_BEGIN
#
# Cloudflare policy:
#
#   1-4 consecutive Cloudflare encounters
#       -> passive wait only
#       -> no user interaction requested
#       -> continue automatically if challenge clears
#
#   5th consecutive Cloudflare encounter
#       -> show dedicated Arena Firefox
#       -> allow manual verification
#       -> continue automatically afterward
#
# A completely clean navigation resets the streak.
#
# This code does NOT solve, click, bypass, or defeat Cloudflare.
# It only waits for Arena/Cloudflare to finish normally.
#

CLOUDFLARE_STREAK_FILE = (
    DEBUG_ROOT
    / "arena-cloudflare-streak.json"
)

CLOUDFLARE_PENDING_FILE = (
    DEBUG_ROOT
    / "arena-cloudflare-pending.txt"
)

CLOUDFLARE_AUTO_WAIT_SECONDS = int(
    os.environ.get(
        "TURNTOAPI_CLOUDFLARE_AUTO_WAIT",
        "180",
    )
)

CLOUDFLARE_HUMAN_AFTER = int(
    os.environ.get(
        "TURNTOAPI_CLOUDFLARE_HUMAN_AFTER",
        "4",
    )
)

CLOUDFLARE_STREAK_EXPIRY_SECONDS = int(
    os.environ.get(
        "TURNTOAPI_CLOUDFLARE_STREAK_EXPIRY",
        "900",
    )
)


def _cloudflare_present(
    driver,
):
    try:
        title = str(
            driver.title
            or ""
        ).lower()

    except Exception:
        title = ""

    body = _body_text(
        driver
    ).lower()

    if "just a moment" in title:
        return True

    markers = (
        "cloudflare",
        "performing security verification",
        "verify you are human",
        "checking your browser",
    )

    for marker in markers:
        if marker in body:
            return True

    return False


def _load_cloudflare_streak():
    try:
        data = json.loads(
            CLOUDFLARE_STREAK_FILE.read_text(
                encoding="utf-8-sig"
            )
        )

        if not isinstance(
            data,
            dict,
        ):
            raise ValueError(
                "Invalid state"
            )

        return data

    except Exception:
        return {
            "consecutive": 0,
            "last_block": 0,
            "status": "clean",
        }


def _save_cloudflare_streak(
    data,
):
    try:
        CLOUDFLARE_STREAK_FILE.write_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    except Exception:
        pass


def _clear_pending_notice():
    try:
        CLOUDFLARE_PENDING_FILE.unlink(
            missing_ok=True
        )

    except Exception:
        pass


def _reset_cloudflare_streak(
    reason="clean-navigation",
):
    _save_cloudflare_streak({
        "consecutive": 0,
        "last_block": 0,
        "status": reason,
        "updated": int(
            time.time()
        ),
    })

    _clear_pending_notice()


def _increment_cloudflare_streak(
    driver,
):
    now = int(
        time.time()
    )

    state = _load_cloudflare_streak()

    previous = int(
        state.get(
            "consecutive",
            0,
        )
        or 0
    )

    last_block = int(
        state.get(
            "last_block",
            0,
        )
        or 0
    )

    # A long gap does not count as "back to back".
    if (
        last_block
        and
        now - last_block
        > CLOUDFLARE_STREAK_EXPIRY_SECONDS
    ):
        previous = 0

    count = (
        previous
        + 1
    )

    try:
        current_url = str(
            driver.current_url
            or ""
        )

    except Exception:
        current_url = ""

    _save_cloudflare_streak({
        "consecutive":
            count,

        "last_block":
            now,

        "status":
            "cloudflare-block",

        "url":
            current_url,

        "human_threshold":
            CLOUDFLARE_HUMAN_AFTER,

        "updated":
            now,
    })

    return count


def wait_for_human_verification(
    driver,
    timeout=600,
):
    challenge = _challenge_present(
        driver
    )

    # A genuinely clean navigation breaks the consecutive streak.
    if not challenge:
        _reset_cloudflare_streak(
            "clean-navigation"
        )

        return True

    is_cloudflare = _cloudflare_present(
        driver
    )

    if is_cloudflare:
        streak = _increment_cloudflare_streak(
            driver
        )

    else:
        # reCAPTCHA / another security mechanism is not counted as a
        # consecutive Cloudflare event.
        streak = 0

        _reset_cloudflare_streak(
            "non-cloudflare-security-challenge"
        )

    human_required = (
        is_cloudflare
        and
        streak
        > CLOUDFLARE_HUMAN_AFTER
    )

    if human_required:

        try:
            driver.maximize_window()

        except Exception:
            pass

        try:
            CLOUDFLARE_PENDING_FILE.write_text(
                (
                    "Arena has hit Cloudflare more than "
                    + str(
                        CLOUDFLARE_HUMAN_AFTER
                    )
                    + " times consecutively.\n\n"
                    + "Consecutive blocks: "
                    + str(
                        streak
                    )
                    + "\n\n"
                    + "Manual verification is now allowed.\n"
                    + "Complete the check in the dedicated Arena Firefox window.\n"
                    + "TurnToAPI will resume automatically when it clears.\n"
                ),
                encoding="utf-8",
            )

        except Exception:
            pass

        wait_seconds = int(
            timeout
        )

        state = _load_cloudflare_streak()

        state[
            "status"
        ] = "human-verification-required"

        state[
            "human_required"
        ] = True

        state[
            "updated"
        ] = int(
            time.time()
        )

        _save_cloudflare_streak(
            state
        )

    else:

        # First four Cloudflare events:
        #
        # Do not maximize the browser.
        # Do not request interaction.
        # Simply pause and let Cloudflare/Arena complete normally.

        _clear_pending_notice()

        wait_seconds = min(
            int(
                timeout
            ),
            CLOUDFLARE_AUTO_WAIT_SECONDS,
        )

        state = _load_cloudflare_streak()

        state[
            "status"
        ] = (
            "passive-cloudflare-wait"
            if is_cloudflare
            else
            "passive-security-wait"
        )

        state[
            "human_required"
        ] = False

        state[
            "updated"
        ] = int(
            time.time()
        )

        _save_cloudflare_streak(
            state
        )

    deadline = (
        time.time()
        + wait_seconds
    )

    while (
        time.time()
        < deadline
    ):

        if not _challenge_present(
            driver
        ):

            _clear_pending_notice()

            if human_required:
                # Human verification broke the repeated-block streak.
                _reset_cloudflare_streak(
                    "human-verification-cleared"
                )

            else:
                # Keep the count temporarily. A following clean navigation
                # will reset it. This is what makes truly back-to-back
                # Cloudflare events accumulate.
                state = _load_cloudflare_streak()

                state[
                    "status"
                ] = (
                    "cloudflare-auto-cleared"
                    if is_cloudflare
                    else
                    "security-auto-cleared"
                )

                state[
                    "updated"
                ] = int(
                    time.time()
                )

                _save_cloudflare_streak(
                    state
                )

            # Allow Arena's client-side router/composer to settle.
            time.sleep(
                1.5
            )

            return True

        time.sleep(
            1
        )

    state = _load_cloudflare_streak()

    state[
        "status"
    ] = (
        "human-verification-timeout"
        if human_required
        else
        "passive-wait-timeout"
    )

    state[
        "updated"
    ] = int(
        time.time()
    )

    _save_cloudflare_streak(
        state
    )

    return False


# TURNTOAPI_CLOUDFLARE_STREAK_V2_END

def _normalise_labels(
    raw_text,
):
    result = []

    for raw in str(
        raw_text or ""
    ).replace(
        "\r",
        ""
    ).split(
        "\n"
    ):

        line = re.sub(
            r"\s+",
            " ",
            raw,
        ).strip()

        if not line:
            continue

        if (
            line.lower()
            in IGNORE_LINES
        ):
            continue

        if len(
            line
        ) > 140:
            continue

        if not MODELISH.match(
            line
        ):
            continue

        if (
            "-" not in line
            and "." not in line
            and not re.search(
                r"\d",
                line,
            )
        ):
            continue

        if line not in result:
            result.append(
                line
            )

    return result


def _open_selector(
    driver,
    mode,
):
    driver.get(
        (
            "https://arena.ai/"
            + mode
            + "/direct?model_a=max"
        )
    )

    time.sleep(
        2
    )

    if not wait_for_human_verification(
        driver
    ):
        raise RuntimeError(
            "Arena security verification did not clear within the configured verification policy window."
        )

    clicked = driver.execute_script(
        r"""
        function visible(el) {
            if (!el) return false;

            const s =
                window.getComputedStyle(el);

            if (
                s.display === "none" ||
                s.visibility === "hidden"
            ) {
                return false;
            }

            const r =
                el.getBoundingClientRect();

            return (
                r.width > 0 &&
                r.height > 0
            );
        }

        function textOf(el) {
            return String(
                el.innerText ||
                el.textContent ||
                ""
            )
            .replace(
                /\s+/g,
                " "
            )
            .trim();
        }

        const candidates =
            Array.from(
                document.querySelectorAll(
                    "button,[role='button'],[role='combobox']"
                )
            )
            .filter(
                el =>
                    visible(el) &&
                    /^Max(?:\s|$)/i.test(
                        textOf(el)
                    )
            );

        if (!candidates.length) {
            return false;
        }

        candidates.sort(
            (a, b) => {
                const ar =
                    a.getBoundingClientRect();

                const br =
                    b.getBoundingClientRect();

                return (
                    br.top -
                    ar.top
                );
            }
        );

        candidates[0].click();

        return true;
        """
    )

    if not clicked:
        raise RuntimeError(
            "Could not open Arena model selector."
        )

    time.sleep(
        0.6
    )


def _selector_state(
    driver,
    advance=False,
):
    return driver.execute_script(
        r"""
        const advance =
            Boolean(arguments[0]);

        function visible(el) {

            if (!el) {
                return false;
            }

            const style =
                getComputedStyle(el);

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

        const input =
            Array.from(
                document.querySelectorAll(
                    "input"
                )
            )
            .find(
                el =>
                    visible(el) &&
                    /search models/i.test(
                        String(
                            el.placeholder ||
                            ""
                        )
                    )
            );

        if (!input) {
            return {
                found: false,
                text: "",
                before: 0,
                after: 0,
                maximum: 0
            };
        }

        let root =
            input.parentElement;

        for (
            let i = 0;
            i < 12 &&
            root;
            i++
        ) {
            const text =
                String(
                    root.innerText ||
                    ""
                );

            const rect =
                root.getBoundingClientRect();

            const lines =
                text
                .split(/\r?\n/)
                .map(
                    x => x.trim()
                )
                .filter(Boolean);

            if (
                rect.width > 250 &&
                rect.width < 1100 &&
                lines.length >= 5
            ) {
                break;
            }

            root =
                root.parentElement;
        }

        if (!root) {
            root =
                input.parentElement;
        }

        const scrollables =
            [
                root,
                ...root.querySelectorAll("*")
            ]
            .filter(
                el =>
                    visible(el) &&
                    el.scrollHeight >
                    el.clientHeight + 20
            );

        scrollables.sort(
            (a, b) =>
                (
                    b.scrollHeight -
                    b.clientHeight
                )
                -
                (
                    a.scrollHeight -
                    a.clientHeight
                )
        );

        const scroller =
            scrollables.length
            ? scrollables[0]
            : root;

        const before =
            Number(
                scroller.scrollTop ||
                0
            );

        const maximum =
            Math.max(
                0,
                Number(
                    scroller.scrollHeight -
                    scroller.clientHeight
                )
            );

        if (
            advance &&
            maximum > before
        ) {
            scroller.scrollTop =
                Math.min(
                    maximum,
                    before +
                    Math.max(
                        200,
                        scroller.clientHeight *
                        0.8
                    )
                );
        }

        return {
            found: true,

            text:
                String(
                    root.innerText ||
                    ""
                ),

            before:
                before,

            after:
                Number(
                    scroller.scrollTop ||
                    0
                ),

            maximum:
                maximum
        };
        """,
        bool(
            advance
        ),
    )


def scan_selector(
    driver,
    mode,
):
    _open_selector(
        driver,
        mode,
    )

    labels = []
    stuck = 0

    for _ in range(
        160
    ):
        state = _selector_state(
            driver,
            False,
        )

        if not state.get(
            "found"
        ):
            break

        for label in _normalise_labels(
            state.get(
                "text",
                ""
            )
        ):
            if label not in labels:
                labels.append(
                    label
                )

        maximum = float(
            state.get(
                "maximum",
                0
            )
            or 0
        )

        before = float(
            state.get(
                "before",
                0
            )
            or 0
        )

        if (
            maximum <= 0
            or before >= maximum - 3
        ):
            break

        moved = _selector_state(
            driver,
            True,
        )

        after = float(
            moved.get(
                "after",
                before
            )
            or before
        )

        if after <= before:
            stuck += 1

            if stuck >= 3:
                break
        else:
            stuck = 0

        time.sleep(
            0.15
        )

    return labels


def _select_exact_label(
    driver,
    mode,
    label,
):
    _open_selector(
        driver,
        mode,
    )

    selected = driver.execute_script(
        r"""
        const wanted =
            String(
                arguments[0] ||
                ""
            ).trim();

        function visible(el) {

            if (!el) {
                return false;
            }

            const style =
                getComputedStyle(el);

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

        function textOf(el) {

            return String(
                el.innerText ||
                el.textContent ||
                ""
            )
            .replace(
                /\s+/g,
                " "
            )
            .trim();
        }

        const input =
            Array.from(
                document.querySelectorAll(
                    "input"
                )
            )
            .find(
                el =>
                    visible(el) &&
                    /search models/i.test(
                        String(
                            el.placeholder ||
                            ""
                        )
                    )
            );

        if (!input) {
            return false;
        }

        const setter =
            Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                "value"
            ).set;

        setter.call(
            input,
            wanted
        );

        input.dispatchEvent(
            new Event(
                "input",
                {
                    bubbles: true
                }
            )
        );

        input.dispatchEvent(
            new Event(
                "change",
                {
                    bubbles: true
                }
            )
        );

        return true;
        """,
        label,
    )

    if not selected:
        return ""

    time.sleep(
        0.8
    )

    clicked = driver.execute_script(
        r"""
        const wanted =
            String(
                arguments[0] ||
                ""
            )
            .replace(
                /\s+/g,
                " "
            )
            .trim()
            .toLowerCase();

        function visible(el) {

            if (!el) {
                return false;
            }

            const style =
                getComputedStyle(el);

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

        function textOf(el) {

            return String(
                el.innerText ||
                el.textContent ||
                ""
            )
            .replace(
                /\s+/g,
                " "
            )
            .trim();
        }

        const candidates =
            Array.from(
                document.querySelectorAll(
                    "button,[role='option'],[role='menuitem'],[role='button'],div"
                )
            )
            .filter(
                el =>
                    visible(el) &&
                    textOf(el)
                        .toLowerCase()
                        === wanted
            );

        candidates.sort(
            (a, b) => {

                function score(el) {

                    let value = 0;

                    if (
                        el.tagName
                            .toLowerCase()
                        === "button"
                    ) {
                        value += 100;
                    }

                    if (
                        el.getAttribute(
                            "role"
                        )
                        === "option"
                    ) {
                        value += 100;
                    }

                    if (
                        el.getAttribute(
                            "role"
                        )
                        === "menuitem"
                    ) {
                        value += 100;
                    }

                    value -=
                        el.children.length;

                    return value;
                }

                return (
                    score(b) -
                    score(a)
                );
            }
        );

        if (!candidates.length) {
            return false;
        }

        let target =
            candidates[0];

        for (
            let i = 0;
            i < 4 &&
            target;
            i++
        ) {
            const role =
                target.getAttribute(
                    "role"
                );

            if (
                target.tagName
                    .toLowerCase()
                === "button"
                ||
                role === "option"
                ||
                role === "menuitem"
            ) {
                break;
            }

            target =
                target.parentElement;
        }

        if (!target) {
            return false;
        }

        target.click();

        return true;
        """,
        label,
    )

    if not clicked:
        return ""

    time.sleep(
        1.5
    )

    if not wait_for_human_verification(
        driver
    ):
        return ""

    actual = _current_route(
        driver
    )

    if (
        not actual
        or actual.lower()
        == "max"
    ):
        return ""

    return actual


def _acquire_lock():
    now = time.time()

    try:
        if LOCK_FILE.exists():
            age = (
                now
                - LOCK_FILE.stat().st_mtime
            )

            if age > 1200:
                LOCK_FILE.unlink(
                    missing_ok=True
                )
    except Exception:
        pass

    try:
        fd = os.open(
            str(
                LOCK_FILE
            ),
            os.O_CREAT |
            os.O_EXCL |
            os.O_WRONLY,
        )

        with os.fdopen(
            fd,
            "w"
        ) as handle:
            handle.write(
                str(
                    os.getpid()
                )
            )

        return True

    except FileExistsError:
        return False


def _release_lock():
    try:
        LOCK_FILE.unlink(
            missing_ok=True
        )
    except Exception:
        pass


def _save_state(
    value,
):
    try:
        STATE_FILE.write_text(
            json.dumps(
                value,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def resolve_single_model(
    driver,
    mode,
    model_id,
):
    entry = get_entry(
        mode,
        model_id,
    )

    if not entry:
        return {
            "status": "unknown",
            "recovered": False,
        }

    label = (
        entry.get(
            "label"
        )
        or entry["id"]
    )

    actual = _select_exact_label(
        driver,
        mode,
        label,
    )

    if not actual:
        return {
            "status": "not-resolved",
            "recovered": False,
        }

    mark_working(
        mode,
        model_id,
        actual,
    )

    return {
        "status": "resolved",
        "recovered": True,
        "route": actual,
    }


def sync_on_verified_max(
    driver,
    failed_mode,
    failed_model,
):
    """
    FULL scanner.

    This is intentionally called only when a registry entry marked
    verified=True unexpectedly redirects to Arena's Max router.
    """

    if not _acquire_lock():
        return {
            "status": "scan-already-running",
            "recovered": False,
        }

    try:
        old = _read_registry()

        state = {
            "started": int(
                time.time()
            ),
            "failed_mode": failed_mode,
            "failed_model": failed_model,
            "status": "running",
        }

        _save_state(
            state
        )

        old_maps = {}

        for mode in (
            "text",
            "code",
        ):
            mode_map = {}

            for entry in _entries(
                mode
            ):
                mode_map[
                    entry["id"].lower()
                ] = entry

                mode_map[
                    entry["label"].lower()
                ] = entry

            old_maps[
                mode
            ] = mode_map

        # Scan the other mode first, then the failed mode.
        # This means successful recovery leaves the browser sitting
        # on the exact model originally requested.
        modes = [
            mode
            for mode in (
                "text",
                "code",
            )
            if mode != failed_mode
        ]

        modes.append(
            failed_mode
        )

        scanned = {}

        for mode in modes:

            labels = scan_selector(
                driver,
                mode,
            )

            scanned[
                mode
            ] = labels

        new_data = {
            "generated": int(
                time.time()
            ),
            "source": (
                "triggered-by-verified-max-redirect"
            ),
            "text": [],
            "code": [],
        }

        for mode in (
            "text",
            "code",
        ):

            previous = old_maps.get(
                mode,
                {}
            )

            seen_ids = set()

            for label in scanned.get(
                mode,
                []
            ):
                existing = previous.get(
                    label.lower()
                )

                if existing:
                    item = {
                        "id":
                            existing["id"],

                        "label":
                            label,

                        "route":
                            existing.get(
                                "route"
                            )
                            or existing["id"],

                        "verified":
                            bool(
                                existing.get(
                                    "verified"
                                )
                            ),
                    }

                else:
                    # New model:
                    #
                    # Advertise it immediately because Arena currently
                    # shows it in the selector, but do NOT call it
                    # previously-working yet.
                    item = {
                        "id":
                            label,

                        "label":
                            label,

                        "route":
                            label,

                        "verified":
                            False,
                    }

                key = item[
                    "id"
                ].lower()

                if key in seen_ids:
                    continue

                seen_ids.add(
                    key
                )

                new_data[
                    mode
                ].append(
                    item
                )

        # Try to recover the exact failed model from the freshly
        # scanned selector.
        failed_entry = get_entry(
            failed_mode,
            failed_model,
        )

        recovery_label = (
            failed_entry.get(
                "label"
            )
            if failed_entry
            else failed_model
        )

        recovery_route = ""

        current_labels = scanned.get(
            failed_mode,
            []
        )

        matched_label = None

        for label in current_labels:
            if (
                label.lower()
                == str(
                    recovery_label
                ).lower()
                or
                label.lower()
                == str(
                    failed_model
                ).lower()
            ):
                matched_label = label
                break

        if matched_label:

            recovery_route = (
                _select_exact_label(
                    driver,
                    failed_mode,
                    matched_label,
                )
            )

        # Remove the broken failed entry if it could not be recovered.
        filtered = []

        for item in new_data[
            failed_mode
        ]:

            if (
                item["id"].lower()
                ==
                str(
                    failed_model
                ).lower()
            ):

                if recovery_route:
                    item[
                        "route"
                    ] = recovery_route

                    item[
                        "verified"
                    ] = True

                    filtered.append(
                        item
                    )

                continue

            filtered.append(
                item
            )

        new_data[
            failed_mode
        ] = filtered

        _write_registry(
            new_data
        )

        result = {
            "status":
                "complete",

            "recovered":
                bool(
                    recovery_route
                ),

            "route":
                recovery_route,

            "text_count":
                len(
                    new_data[
                        "text"
                    ]
                ),

            "code_count":
                len(
                    new_data[
                        "code"
                    ]
                ),

            "failed_model_removed":
                not bool(
                    recovery_route
                ),
        }

        _save_state({
            **state,
            **result,
            "completed":
                int(
                    time.time()
                ),
        })

        return result

    except Exception as exc:

        _save_state({
            "started":
                int(
                    time.time()
                ),

            "failed_mode":
                failed_mode,

            "failed_model":
                failed_model,

            "status":
                "error",

            "error":
                repr(
                    exc
                ),
        })

        raise

    finally:
        _release_lock()