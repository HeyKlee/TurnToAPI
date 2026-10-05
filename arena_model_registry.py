from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = Path(
    os.environ.get("TURNTOAPI_ARENA_REGISTRY", ROOT / "arena_models_live.json")
)

_LOCK = threading.RLock()


def _blank() -> dict[str, Any]:
    return {"schema_version": 1, "text": [], "code": []}


def load_registry() -> dict[str, Any]:
    with _LOCK:
        if not REGISTRY_PATH.exists():
            return _blank()
        with REGISTRY_PATH.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise RuntimeError("Arena registry is not a JSON object.")
        data.setdefault("schema_version", 1)
        data.setdefault("text", [])
        data.setdefault("code", [])
        return data


def save_registry(data: dict[str, Any]) -> None:
    with _LOCK:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=REGISTRY_PATH.name + ".",
            suffix=".tmp",
            dir=str(REGISTRY_PATH.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temp_name, REGISTRY_PATH)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def list_models(mode: str) -> list[dict[str, Any]]:
    mode = "code" if mode == "code" else "text"
    data = load_registry()
    result: list[dict[str, Any]] = []
    for raw in data.get(mode, []):
        if isinstance(raw, str):
            item = {
                "id": raw,
                "label": raw,
                "route": raw,
                "verified": False,
            }
        elif isinstance(raw, dict):
            model_id = str(raw.get("id", "")).strip()
            if not model_id:
                continue
            item = {
                "id": model_id,
                "label": str(raw.get("label") or model_id),
                "route": str(raw.get("route") or model_id),
                "verified": bool(raw.get("verified", False)),
            }
        else:
            continue

        if item["id"].lower() == "max":
            continue
        result.append(item)
    return result


def get_model(mode: str, model_id: str) -> dict[str, Any] | None:
    wanted = str(model_id or "").strip()
    for item in list_models(mode):
        if item["id"] == wanted:
            return item
    return None


def get_route(mode: str, model_id: str) -> str | None:
    item = get_model(mode, model_id)
    if not item:
        return None
    return str(item.get("route") or item["id"]).strip() or None


def update_model(
    mode: str,
    model_id: str,
    *,
    label: str | None = None,
    route: str | None = None,
    verified: bool | None = None,
) -> dict[str, Any]:
    mode = "code" if mode == "code" else "text"
    model_id = str(model_id or "").strip()
    if not model_id or model_id.lower() == "max":
        raise ValueError("Invalid Arena model ID.")

    with _LOCK:
        data = load_registry()
        items = list(data.get(mode, []))
        updated = None

        for index, raw in enumerate(items):
            current_id = (
                raw if isinstance(raw, str) else str(raw.get("id", ""))
            ).strip()
            if current_id != model_id:
                continue

            current = {
                "id": model_id,
                "label": model_id,
                "route": model_id,
                "verified": False,
            }
            if isinstance(raw, dict):
                current.update(raw)

            if label is not None:
                current["label"] = str(label)
            if route is not None:
                current["route"] = str(route)
            if verified is not None:
                current["verified"] = bool(verified)

            items[index] = current
            updated = current
            break

        if updated is None:
            updated = {
                "id": model_id,
                "label": str(label or model_id),
                "route": str(route or model_id),
                "verified": bool(verified) if verified is not None else False,
            }
            items.append(updated)

        data[mode] = items
        save_registry(data)
        return updated


def mark_verified(mode: str, model_id: str, route: str | None = None) -> None:
    update_model(mode, model_id, route=route, verified=True)


def mark_unverified(mode: str, model_id: str) -> None:
    update_model(mode, model_id, verified=False)


def remove_model(mode: str, model_id: str) -> bool:
    mode = "code" if mode == "code" else "text"
    model_id = str(model_id or "").strip()

    with _LOCK:
        data = load_registry()
        before = list(data.get(mode, []))
        after = []
        removed = False

        for raw in before:
            current_id = (
                raw if isinstance(raw, str) else str(raw.get("id", ""))
            ).strip()
            if current_id == model_id:
                removed = True
                continue
            after.append(raw)

        if removed:
            data[mode] = after
            save_registry(data)
        return removed
