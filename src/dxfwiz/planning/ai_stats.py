from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


DEFAULT_STATS_PATH = Path("workspace") / "ai_stats.json"


@dataclass(frozen=True)
class AiCallStats:
    model: str
    elapsed_seconds: float
    prompt_tokens: int
    completion_tokens: int
    success: bool
    error: str | None = None


def record_ai_call(stats: AiCallStats, path: str | Path = DEFAULT_STATS_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_stats(path)
    calls = data.setdefault("calls", [])
    calls.append(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "model": stats.model,
            "elapsed_seconds": stats.elapsed_seconds,
            "prompt_tokens": stats.prompt_tokens,
            "completion_tokens": stats.completion_tokens,
            "total_tokens": stats.prompt_tokens + stats.completion_tokens,
            "success": stats.success,
            "error": stats.error,
        }
    )
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ai_stats_summary(token_limit: int, path: str | Path = DEFAULT_STATS_PATH) -> dict[str, Any]:
    data = _read_stats(Path(path))
    calls = data.get("calls", [])
    total_tokens = sum(int(call.get("total_tokens") or 0) for call in calls)
    successful = [call for call in calls if call.get("success")]
    elapsed_values = [float(call.get("elapsed_seconds") or 0) for call in calls]
    return {
        "token_limit": token_limit,
        "total_tokens": total_tokens,
        "remaining_tokens": max(token_limit - total_tokens, 0),
        "token_percent": (total_tokens / token_limit * 100) if token_limit else 0,
        "call_count": len(calls),
        "success_count": len(successful),
        "failure_count": len(calls) - len(successful),
        "average_response_seconds": (
            sum(elapsed_values) / len(elapsed_values) if elapsed_values else 0
        ),
        "last_call": calls[-1] if calls else None,
    }


def _read_stats(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"calls": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read AI stats from %s; starting fresh", path)
        return {"calls": []}
    if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
        return {"calls": []}
    return data
