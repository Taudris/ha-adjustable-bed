"""Shared utilities for diagnostics and support reports."""

from __future__ import annotations

from statistics import median
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .coordinator import AdjustableBedCoordinator

# The tightest control-box keep-alive window known among the beds that stream a
# held keycode. Measured on a Leggett & Platt Okin CU170 over an ESPHome proxy:
# an achieved inter-frame gap of p50 232 / max 237 ms moved the bed smoothly
# apart from occasional stutter, so the box gives up on a hold just under
# ~237 ms of silence. A gap past this is what "the stream fell behind" means to
# a reader, which is why it is a histogram edge and not a threshold buried in
# prose.
STREAM_GAP_WATCHDOG_MS: Final = 235

# Upper bounds of the gap histogram buckets, in milliseconds. They are chosen so
# the watchdog is an exact edge: everything at or below STREAM_GAP_WATCHDOG_MS
# is a gap the bed tolerated, everything above it is one it may not have. The
# buckets below that edge separate "on cadence" from "slipping but safe"; those
# above it separate a marginal breach from a stall.
_GAP_BUCKET_BOUNDS_MS: Final = (100, 150, 200, STREAM_GAP_WATCHDOG_MS, 300, 500)


def _gap_bucket_labels() -> tuple[str, ...]:
    """Return the histogram bucket labels, in order."""
    labels = []
    lower = 0
    for bound in _GAP_BUCKET_BOUNDS_MS:
        labels.append(f"{lower}-{bound}" if lower else f"<={bound}")
        lower = bound + 1
    labels.append(f">{_GAP_BUCKET_BOUNDS_MS[-1]}")
    return tuple(labels)


def summarize_stream_gaps(gaps_ms: list[float]) -> dict[str, Any]:
    """Summarize the gaps between consecutive frames of one streamed command.

    The gap between frames is the only cadence figure that means anything to
    this hardware: a control box retriggers its keep-alive on the last frame it
    received, so what breaks a hold is one gap that ran long, not how far the
    stream has drifted from the schedule it started on.

    Gaps are bucketed rather than listed because a stream can be hundreds of
    frames long and the question a reader has is how many of them the bed could
    have noticed. ``breaches`` answers that directly instead of leaving them to
    add up buckets.
    """
    labels = _gap_bucket_labels()
    histogram = dict.fromkeys(labels, 0)
    for gap_ms in gaps_ms:
        index = next(
            (i for i, bound in enumerate(_GAP_BUCKET_BOUNDS_MS) if gap_ms <= bound),
            len(_GAP_BUCKET_BOUNDS_MS),
        )
        histogram[labels[index]] += 1
    return {
        "gap_count": len(gaps_ms),
        "median_gap_ms": round(median(gaps_ms), 1) if gaps_ms else None,
        "max_gap_ms": round(max(gaps_ms), 1) if gaps_ms else None,
        "breach_threshold_ms": STREAM_GAP_WATCHDOG_MS,
        "breaches": sum(1 for gap_ms in gaps_ms if gap_ms > STREAM_GAP_WATCHDOG_MS),
        "histogram_ms": histogram,
    }


def get_gatt_summary(coordinator: AdjustableBedCoordinator) -> dict[str, Any]:
    """Get GATT service/characteristic summary for diagnostics."""
    client = coordinator.client
    if not client or not client.services:
        return {"available": False}

    # Capture services once to avoid multiple iterations
    services = list(client.services)
    char_count = 0
    notifiable_chars: list[str] = []
    writable_chars: list[str] = []

    for service in services:
        for char in service.characteristics:
            char_count += 1
            if "notify" in char.properties or "indicate" in char.properties:
                notifiable_chars.append(char.uuid)
            if "write" in char.properties or "write-without-response" in char.properties:
                writable_chars.append(char.uuid)

    return {
        "available": True,
        "service_count": len(services),
        "characteristic_count": char_count,
        "notifiable_characteristics": sorted(notifiable_chars),
        "writable_characteristics": sorted(writable_chars),
    }
