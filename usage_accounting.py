"""Price direct plugin calls without recording prompts, audio, or credentials.

Each plugin vendors this file so its worker needs no new Python dependency.
Set MAIBOT_BILLING_CATALOG and MAIBOT_BILLING_LEDGER to share an external ledger.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import asyncio
import json
import math
import os
import sqlite3
import uuid


CATALOG_PATH = Path(os.environ.get("MAIBOT_BILLING_CATALOG", str(Path(__file__).resolve().parent / "data" / "model-billing" / "prices.json")))
LEDGER_PATH = Path(os.environ.get("MAIBOT_BILLING_LEDGER", str(Path(__file__).resolve().parent / "data" / "model-billing" / "usage.sqlite3")))
VERSION = "1.0.0"
_COUNTER_KEYS = {
    "input_tokens", "output_tokens", "total_tokens", "prompt_tokens",
    "completion_tokens", "cached_tokens", "text_tokens", "image_tokens",
    "audio_tokens", "input_tokens_details", "output_tokens_details",
    "prompt_tokens_details", "completion_tokens_details", "cached_tokens_details",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "utf8_bytes",
    "characters", "credits", "anlas", "requests", "audio_seconds",
    "reference_images", "credit_multiplier", "steps", "width", "height",
    "input_kind", "size", "effort", "n_samples",
}


def _safe_counts(value: Any) -> dict[str, Any]:
    """Retain only billing counters; never persist arbitrary response fields."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _COUNTER_KEYS or isinstance(item, bool):
            continue
        if isinstance(item, dict):
            result[key] = _safe_counts(item)
        elif isinstance(item, (int, float)) and math.isfinite(item) and item >= 0:
            result[key] = item
        elif key in {"input_kind", "size", "effort"} and isinstance(item, str):
            result[key] = item[:64]
    return result


def _tokens(usage: dict[str, Any]) -> tuple[int, int, int]:
    incoming = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
    outgoing = int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
    return incoming, outgoing, int(usage.get("total_tokens", incoming + outgoing))


def calculate_cost(
    catalog: dict[str, Any], provider: str, model: str,
    usage: dict[str, Any], units: dict[str, Any],
) -> tuple[float | None, str, dict[str, Any]]:
    """Return CNY, valuation basis and source rate; unknown is never zero."""
    rate = catalog["rates"].get(f"{provider}/{model}")
    if rate is None:
        return None, "missing_price", {}
    currency_factor = 1.0 if rate["currency"] == "CNY" else float(catalog["usd_cny"])
    kind = rate["kind"]
    incoming, outgoing, _ = _tokens(usage)
    if kind == "free":
        return 0.0, "official_free_promotion", rate
    if kind == "tokens":
        if not usage:
            return None, "missing_usage", rate
        detail = usage.get("input_tokens_details", usage.get("prompt_tokens_details", {}))
        cached = int(detail.get("cached_tokens", usage.get("prompt_cache_hit_tokens", 0)))
        if cached > incoming:
            return None, "invalid_cache_usage", rate
        amount = ((incoming - cached) * rate["input"] + cached * rate["cached_input"]
                  + outgoing * rate["output"]) / 1_000_000
    elif kind == "image_tokens":
        if not usage or "output_tokens" not in usage:
            return None, "missing_image_usage", rate
        detail = usage.get("input_tokens_details", {})
        if "text_tokens" in detail and "image_tokens" in detail:
            text = int(detail["text_tokens"])
            images = int(detail["image_tokens"])
            if text + images != incoming:
                return None, "inconsistent_image_usage", rate
        elif units.get("input_kind") == "text" and units.get("reference_images", 0) == 0:
            text, images = incoming, 0
        else:
            return None, "missing_image_input_breakdown", rate
        cached = int(detail.get("cached_tokens", 0))
        cache_detail = detail.get("cached_tokens_details", {})
        cached_text = int(cache_detail.get("text_tokens", 0))
        cached_images = int(cache_detail.get("image_tokens", 0))
        if cached_text + cached_images != cached:
            # A single cached total cannot establish two different image/text prices.
            return None, "missing_image_cache_breakdown", rate
        if cached_text > text or cached_images > images:
            return None, "invalid_image_cache_usage", rate
        amount = ((text - cached_text) * rate["text_input"]
                  + (images - cached_images) * rate["image_input"]
                  + cached_text * rate["cached_text_input"]
                  + cached_images * rate["cached_image_input"]
                  + outgoing * rate["image_output"]) / 1_000_000
    else:
        unit_key = {"utf8_bytes": "utf8_bytes", "characters": "characters",
                    "requests": "requests", "audio_seconds": "audio_seconds",
                    "anlas": "anlas"}[kind]
        if unit_key not in units:
            return None, f"missing_{unit_key}", rate
        amount = float(units[unit_key]) * float(rate["per_unit"])
    return round(amount * currency_factor, 6), "official_list_price", rate


def _write_ledger(record: dict[str, Any], status: str, error: str = "") -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(LEDGER_PATH, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS usage_events ("
            "event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, provider TEXT NOT NULL,"
            "model TEXT NOT NULL, request_type TEXT NOT NULL, cost_cny REAL,"
            "billing_mode TEXT NOT NULL, basis TEXT NOT NULL, status TEXT NOT NULL,"
            "details_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,error=excluded.error",
            (record["event_id"], record["timestamp"], record["provider"], record["model"],
             record["request_type"], record["cost_cny"], record["billing_mode"],
             record["basis"], status, json.dumps(record["details"], ensure_ascii=False), error[:300]),
        )


async def record_usage(
    ctx: Any, *, provider: str, model: str, request_type: str,
    usage: dict[str, Any] | None = None, units: dict[str, Any] | None = None,
    session_id: str = "", time_cost: float = 0.0, billing_mode: str = "api",
) -> dict[str, Any]:
    """Audit a paid response, then send its measured cost to MaiBot statistics.

    A delivery/download error must not erase an already incurred generation cost.
    Database failures are visible in both logs and the ledger, without causing a
    second generation. Ambiguous database writes are never automatically retried.
    """
    event: dict[str, Any] = {}
    try:
        catalog = await asyncio.to_thread(
            lambda: json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        )
        safe_usage, safe_units = _safe_counts(usage), _safe_counts(units)
        cost, basis, rate = calculate_cost(catalog, provider, model, safe_usage, safe_units)
        incoming, outgoing, total = _tokens(safe_usage)
        event = {
            "event_id": uuid.uuid4().hex,
            "timestamp": datetime.now().astimezone().isoformat(),
            "provider": provider, "model": model, "request_type": request_type,
            "cost_cny": cost, "billing_mode": billing_mode, "basis": basis,
            "details": {"usage": safe_usage, "units": safe_units, "rate": rate,
                        "usd_cny": catalog["usd_cny"], "catalog_version": catalog["version"]},
        }
        await asyncio.to_thread(_write_ledger, event, "pending" if cost is not None else "unpriced")
        if cost is None:
            ctx.logger.warning(f"费用待核算: {provider}/{model}: {basis}; event={event['event_id']}")
            return {"success": False, "unpriced": True, "event_id": event["event_id"], "reason": basis}
        provider_label = catalog.get("provider_labels", {}).get(provider, provider)
        suffix = "（API等价估值）" if billing_mode == "api_equivalent" else ""
        data = {
            "model_name": model,
            "model_assign_name": f"{model}{suffix}",
            "model_api_provider_name": f"{provider_label}{suffix}",
            "session_id": session_id.strip(),
            "task_name": "image_generation" if "image" in request_type else
                         "web_search" if "search" in request_type or "fetch" in request_type else
                         "tts" if "tts" in request_type else "plugin_vision",
            "request_type": request_type[:50],
            "time_cost": round(max(float(time_cost), 0.0), 3),
            "prompt_tokens": incoming, "completion_tokens": outgoing, "total_tokens": total,
            "prompt_cache_enabled": False,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
            "cost": cost,
        }
        result = await ctx.db.save(model_name="ModelUsage", data=data)
        # database.save returns the saved ModelUsage row, not a success envelope.
        if not isinstance(result, dict) or result.get("id") is None or result.get("success") is False:
            raise RuntimeError("MaiBot database.save did not confirm success")
        await asyncio.to_thread(_write_ledger, event, "recorded")
        return {"success": True, "cost_cny": cost, "event_id": event["event_id"]}
    except Exception as exc:
        if event:
            try:
                await asyncio.to_thread(_write_ledger, event, "save_failed", type(exc).__name__)
            except Exception:
                pass
        ctx.logger.error(f"费用记录失败: {provider}/{model}: {type(exc).__name__}; 请检查费用账本")
        return {"success": False, "error": type(exc).__name__}
