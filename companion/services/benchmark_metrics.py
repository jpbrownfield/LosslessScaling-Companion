"""Parse and summarize PresentMon captures for LS performance comparisons."""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_ALIASES = {
    "frame_time_ms": ("FrameTime", "MsBetweenPresents"),
    "displayed_time_ms": ("DisplayedTime", "MsBetweenDisplayChange"),
    "display_latency_ms": ("DisplayLatency", "MsUntilDisplayed"),
    "click_to_visible_ms": ("MsClickToPhotonLatency", "ClickToPhotonLatency"),
    "all_input_to_visible_ms": (
        "MsAllInputToPhotonLatency",
        "AllInputToPhotonLatency",
    ),
    "gpu_busy_ms": ("GPUBusy", "MsGPUBusy", "msGPUActive"),
}


def _number(value: object) -> Optional[float]:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _first(row: Dict[str, str], names: Iterable[str]) -> Optional[float]:
    for name in names:
        value = _number(row.get(name))
        if value is not None:
            return value
    return None


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_values(values: Iterable[float]) -> Dict[str, Optional[float]]:
    valid = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(valid),
        "mean": statistics.fmean(valid) if valid else None,
        "median": statistics.median(valid) if valid else None,
        "p95": _percentile(valid, 0.95),
        "p99": _percentile(valid, 0.99),
        "min": min(valid) if valid else None,
        "max": max(valid) if valid else None,
    }


def read_presentmon_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def summarize_presentmon_rows(
    rows: Iterable[Dict[str, str]], *, warmup_seconds: float = 0.0
) -> Dict:
    selected = list(rows)
    if warmup_seconds > 0 and selected:
        time_names = ("TimeInSeconds", "CPUStartTime", "CPUStartTimeInSeconds")
        times = [_first(row, time_names) for row in selected]
        available = [value for value in times if value is not None]
        if available:
            cutoff = min(available) + warmup_seconds
            selected = [
                row for row, value in zip(selected, times)
                if value is not None and value >= cutoff
            ]

    application = next(
        (row.get("Application") for row in selected if row.get("Application")), None
    )
    process_id = next(
        (int(value) for row in selected for value in [row.get("ProcessID", "")]
         if value.isdigit()),
        None,
    )
    metrics = {}
    for output_name, aliases in _ALIASES.items():
        values = [value for row in selected for value in [_first(row, aliases)] if value is not None]
        metrics[output_name] = summarize_values(values)

    displayed = metrics["displayed_time_ms"]
    frame_time = displayed if displayed["count"] else metrics["frame_time_ms"]
    median_ms = frame_time["median"]
    metrics["displayed_fps"] = {
        "median": 1000.0 / median_ms if median_ms else None,
        "one_percent_low": (
            1000.0 / frame_time["p99"] if frame_time["p99"] else None
        ),
    }
    return {
        "application": application,
        "process_id": process_id,
        "rows": len(selected),
        "metrics": metrics,
        "latency_scope": {
            "click_to_visible_ms": (
                "PresentMon software input-to-displayed-frame correlation for this process; "
                "it is not physical click-to-photon latency"
            ),
            "display_latency_ms": (
                "This process's frame-start/present-to-display interval; it does not by itself "
                "join source-application input to Lossless Scaling output"
            ),
        },
    }


def summarize_presentmon_files(
    paths: Iterable[Path], *, warmup_seconds: float = 0.0
) -> List[Dict]:
    summaries = []
    for path in sorted(paths, key=lambda item: item.name.casefold()):
        summary = summarize_presentmon_rows(
            read_presentmon_csv(path), warmup_seconds=warmup_seconds
        )
        summary["csv"] = str(path)
        summaries.append(summary)
    return summaries


__all__ = [
    "read_presentmon_csv",
    "summarize_presentmon_files",
    "summarize_presentmon_rows",
    "summarize_values",
]
