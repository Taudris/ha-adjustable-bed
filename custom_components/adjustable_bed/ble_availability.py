"""BLE availability telemetry for the Adjustable Bed integration.

Home Assistant's Bluetooth stack already knows when a bed last advertised, how
strong that advertisement was, and which adapter or proxy heard it. Connection
failures - especially ESP_GATT status 133 through ESPHome proxies - are hard to
interpret without that context: a bed that stopped advertising while another
central held it looks exactly like a bed that is simply out of range.

This module records that context and makes it available to connection logging,
diagnostic entities, and the diagnostics download. It is observation only:
nothing here gates, delays, or otherwise changes connection behaviour.

Availability is judged from plural signals: the bed is available while this
integration holds its connection OR while advertisements are recent. A
single-central bed stops advertising entirely while connected, so advertisement
absence during our own connection is expected, not an availability problem.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .adapter import find_service_info_by_address

_LOGGER = logging.getLogger(__name__)

# Beds advertise several times per second. Entity state writes are throttled to
# this interval so the recorder does not fill with BLE noise; availability
# transitions bypass the throttle because those are the interesting events.
# This is a recorder-noise budget and nothing else: no staleness or availability
# threshold may be derived from it, or a future recorder-motivated change would
# silently move that threshold too.
ADVERTISEMENT_UPDATE_INTERVAL = 30.0

# After we release our own connection the bed needs time to resume advertising
# and a scanner needs to hear it, so advertisement absence within this window is
# attributed to the just-finished connection rather than to the bed.
#
# This grace is added on top of the Bluetooth stack's own latency, never instead
# of it: the timer is armed only once the stack has already called us back as
# unavailable. For a connectable address habluetooth applies no advertising-
# interval test at all - the address goes unavailable only once it has dropped
# out of every scanner's discovered set. Scanners drop entries older than
# CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS (195 s) on a 30 s
# sweep, and the manager's unavailable sweep reschedules itself every
# UNAVAILABLE_TRACK_SECONDS (300 s), so the stack's verdict lands roughly 195 to
# 525 s after the last advertisement - three to nine minutes, not one.
#
# What the grace buys is therefore accuracy, not timeliness: it stops a
# disconnect that happens to land just before the stack's sweep from being
# blamed on the bed. One minute is ample for that, and is deliberately its own
# number rather than a multiple of anything else in this module.
POST_DISCONNECT_ADVERTISEMENT_GRACE = 60.0


@dataclass(frozen=True)
class AdvertisementTelemetry:
    """What Home Assistant's Bluetooth stack currently knows about a device."""

    seen: bool
    age_seconds: float | None
    rssi: int | None
    source: str | None
    connectable: bool | None

    def as_log_fragment(self) -> str:
        """Return a phrase describing device visibility for a log message.

        Written to slot into an existing sentence, so a connection failure line
        states how long ago the bed was advertising, which adapter or proxy
        heard it, and at what signal strength.
        """
        if not self.seen or self.age_seconds is None:
            return "no advertisement on record"

        rssi = "unknown" if self.rssi is None else self.rssi
        connectable = "connectable" if self.connectable else "non-connectable"
        return (
            f"last advertisement {self.age_seconds:.1f}s ago via {self.source or 'unknown'} "
            f"at {rssi} dBm ({connectable})"
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the telemetry as a diagnostics-friendly mapping."""
        return {
            "seen": self.seen,
            "age_seconds": self.age_seconds,
            "rssi": self.rssi,
            "source": self.source,
            "connectable": self.connectable,
        }


_UNKNOWN_TELEMETRY = AdvertisementTelemetry(
    seen=False,
    age_seconds=None,
    rssi=None,
    source=None,
    connectable=None,
)


@callback
def async_advertisement_telemetry(hass: HomeAssistant, address: str) -> AdvertisementTelemetry:
    """Return what the Bluetooth stack last heard from ``address``.

    Falls back to the non-connectable scanner view, because a proxy that
    misclassifies the bed still proves it was advertising. Never raises: the
    Bluetooth integration may not be loaded, and telemetry must not be able to
    break a connection attempt it is only meant to describe.
    """
    try:
        service_info, connectable = find_service_info_by_address(
            hass,
            address,
            allow_non_connectable=True,
        )
    except Exception as err:  # Telemetry is best-effort; never fail the caller
        _LOGGER.debug("Could not read advertisement telemetry for %s: %s", address, err)
        return _UNKNOWN_TELEMETRY

    if service_info is None:
        return _UNKNOWN_TELEMETRY

    # BluetoothServiceInfoBleak.time is a monotonic timestamp (the same epoch as
    # time.monotonic), set when the advertisement was received.
    advertisement_time = getattr(service_info, "time", None)
    age_seconds: float | None = None
    if isinstance(advertisement_time, int | float):
        age_seconds = round(max(0.0, time.monotonic() - float(advertisement_time)), 1)

    rssi = getattr(service_info, "rssi", None)
    return AdvertisementTelemetry(
        seen=True,
        age_seconds=age_seconds,
        rssi=rssi if isinstance(rssi, int) else None,
        source=getattr(service_info, "source", None),
        connectable=connectable,
    )


class BleAvailabilityTracker:
    """Track Home Assistant's view of one bed's BLE visibility.

    Subscribes to the bed's advertisements and to Home Assistant's unavailable
    tracking, so the log records when the stack stops seeing the bed and when it
    reappears. Diagnostic entities read the last advertisement and RSSI from
    here rather than each polling the Bluetooth stack themselves.
    """

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        """Initialize the tracker for a configured address."""
        self.hass = hass
        self._address = address.upper()
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._listeners: set[Callable[[], None]] = set()
        self._unavailable = False
        self._connected = False
        self._stack_unavailable = False
        self._last_disconnect_monotonic: float | None = None
        self._cancel_grace: Callable[[], None] | None = None
        self._last_advertisement: datetime | None = None
        self._last_rssi: int | None = None
        self._last_source: str | None = None
        self._last_unavailable_at: datetime | None = None
        self._last_available_at: datetime | None = None
        self._unavailable_transitions = 0
        self._last_listener_notify: float | None = None

    @property
    def tracking(self) -> bool:
        """Return True while the Bluetooth subscriptions are registered."""
        return bool(self._cancel_callbacks)

    @property
    def unavailable(self) -> bool:
        """Return True when the bed is judged genuinely unavailable.

        Judged, not raw: we do not hold its connection AND advertisements
        stayed absent past the post-disconnect grace. Never True while this
        integration is connected to the bed.
        """
        return self._unavailable

    @property
    def last_advertisement(self) -> datetime | None:
        """Return when the bed was last heard advertising."""
        return self._last_advertisement

    @property
    def last_rssi(self) -> int | None:
        """Return the signal strength of the last advertisement."""
        return self._last_rssi

    @property
    def last_source(self) -> str | None:
        """Return the adapter or proxy that heard the last advertisement."""
        return self._last_source

    @callback
    def async_start(self) -> Callable[[], None]:
        """Subscribe to Bluetooth events and return the unsubscribe callback."""
        if self._cancel_callbacks:
            return self.async_stop

        self._async_seed_from_scanner_history()

        try:
            self._cancel_callbacks.append(
                bluetooth.async_track_unavailable(
                    self.hass,
                    self._async_handle_unavailable,
                    self._address,
                    connectable=True,
                )
            )
            self._cancel_callbacks.append(
                bluetooth.async_register_callback(
                    self.hass,
                    self._async_handle_advertisement,
                    BluetoothCallbackMatcher(address=self._address, connectable=True),
                    BluetoothScanningMode.PASSIVE,
                )
            )
        except Exception as err:  # Telemetry must never block integration setup
            _LOGGER.debug(
                "BLE availability tracking unavailable for %s: %s",
                self._address,
                err,
            )
            self.async_stop()

        return self.async_stop

    @callback
    def async_stop(self) -> None:
        """Unsubscribe from Bluetooth events. Safe to call more than once."""
        self._async_cancel_grace()
        while self._cancel_callbacks:
            self._cancel_callbacks.pop()()

    @callback
    def async_set_connected(self, connected: bool) -> None:
        """Record whether this integration itself holds the bed's connection.

        The bed stops advertising while connected, so the tracker must know
        when advertisement absence is our own doing: while connected it is
        expected, and after we disconnect the bed gets
        POST_DISCONNECT_ADVERTISEMENT_GRACE to resume advertising before
        absence counts against it.
        """
        if connected == self._connected:
            return
        self._connected = connected

        if connected:
            self._async_cancel_grace()
            if self._unavailable:
                # We hold the connection, so the bed is definitively reachable.
                self._unavailable = False
                self._last_available_at = datetime.now(UTC)
                _LOGGER.info(
                    "Bed %s is connected; clearing its unavailable state",
                    self._address,
                )
                self._async_notify_listeners()
            return

        self._last_disconnect_monotonic = time.monotonic()
        if self._stack_unavailable:
            # The stack stopped seeing advertisements while we held the
            # connection (expected, and suppressed at the time). Re-judge once
            # the bed has had time to resume advertising.
            self._async_schedule_grace_check(POST_DISCONNECT_ADVERTISEMENT_GRACE)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a throttled update listener and return its unregister callback."""
        self._listeners.add(listener)

        def unregister() -> None:
            self._listeners.discard(listener)

        return unregister

    @property
    def telemetry(self) -> AdvertisementTelemetry:
        """Return the current advertisement telemetry from the Bluetooth stack."""
        return async_advertisement_telemetry(self.hass, self._address)

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return availability state for the diagnostics download."""
        return {
            "tracking": self.tracking,
            "unavailable": self._unavailable,
            "connected": self._connected,
            "stack_unavailable": self._stack_unavailable,
            "unavailable_transitions": self._unavailable_transitions,
            "last_advertisement": (
                self._last_advertisement.isoformat() if self._last_advertisement else None
            ),
            "last_rssi": self._last_rssi,
            "last_source": self._last_source,
            "last_unavailable_at": (
                self._last_unavailable_at.isoformat() if self._last_unavailable_at else None
            ),
            "last_available_at": (
                self._last_available_at.isoformat() if self._last_available_at else None
            ),
            "current": self.telemetry.as_dict(),
        }

    @callback
    def _async_seed_from_scanner_history(self) -> None:
        """Adopt the scanner's existing record so entities start with a value.

        Without this, both diagnostic sensors stay unknown until the next
        advertisement, which for a slowly-advertising bed can be minutes.
        """
        telemetry = self.telemetry
        if not telemetry.seen or telemetry.age_seconds is None:
            return
        self._last_advertisement = datetime.now(UTC) - timedelta(seconds=telemetry.age_seconds)
        self._last_rssi = telemetry.rssi
        self._last_source = telemetry.source

    @callback
    def _async_handle_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Record an advertisement and, on the first one after a gap, log recovery."""
        del change  # BluetoothChange only has ADVERTISEMENT
        self._stack_unavailable = False
        self._async_cancel_grace()
        self._last_advertisement = datetime.now(UTC)
        rssi = getattr(service_info, "rssi", None)
        self._last_rssi = rssi if isinstance(rssi, int) else None
        self._last_source = getattr(service_info, "source", None)

        if self._unavailable:
            self._unavailable = False
            self._last_available_at = self._last_advertisement
            _LOGGER.info(
                "Bed %s is advertising again via %s at %s dBm (unavailable since %s)",
                self._address,
                self._last_source or "unknown",
                self._last_rssi if self._last_rssi is not None else "unknown",
                self._last_unavailable_at.isoformat() if self._last_unavailable_at else "unknown",
            )
            self._async_notify_listeners()
            return

        self._async_notify_listeners_throttled()

    @callback
    def _async_handle_unavailable(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Judge advertisement absence against our own connection state."""
        del service_info  # The last-known advertisement is already recorded
        self._stack_unavailable = True
        if self._unavailable:
            return

        if self._connected:
            # Expected: the bed cannot advertise while we hold its connection.
            _LOGGER.debug(
                "Bed %s stopped advertising while this integration holds its "
                "connection; expected, not an availability problem",
                self._address,
            )
            return

        remaining_grace = self._grace_remaining()
        if remaining_grace is not None:
            # We disconnected moments ago; give the bed the rest of the grace
            # window to resume advertising before judging it gone.
            self._async_schedule_grace_check(remaining_grace)
            return

        self._async_declare_unavailable()

    def _grace_remaining(self) -> float | None:
        """Return seconds left in the post-disconnect grace window, if any."""
        if self._last_disconnect_monotonic is None:
            return None
        elapsed = time.monotonic() - self._last_disconnect_monotonic
        remaining = POST_DISCONNECT_ADVERTISEMENT_GRACE - elapsed
        return remaining if remaining > 0 else None

    @callback
    def _async_schedule_grace_check(self, delay: float) -> None:
        """Arrange a re-judgement after the post-disconnect grace elapses."""
        if self._cancel_grace is not None:
            return
        self._cancel_grace = async_call_later(self.hass, delay, self._async_grace_expired)

    @callback
    def _async_cancel_grace(self) -> None:
        """Cancel any pending post-disconnect grace check."""
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None

    @callback
    def _async_grace_expired(self, _fired_at: datetime) -> None:
        """Declare the bed unavailable if it never resumed advertising."""
        self._cancel_grace = None
        if self._connected or not self._stack_unavailable:
            return
        self._async_declare_unavailable()

    @callback
    def _async_declare_unavailable(self) -> None:
        """Record and log that the bed is genuinely unavailable, once."""
        if self._unavailable:
            return

        self._unavailable = True
        self._unavailable_transitions += 1
        self._last_unavailable_at = datetime.now(UTC)
        _LOGGER.info(
            "Home Assistant no longer sees advertisements from bed %s "
            "(last advertisement %s via %s at %s dBm). The bed may be powered off, out of "
            "range, or holding a connection to another device",
            self._address,
            self._last_advertisement.isoformat() if self._last_advertisement else "never",
            self._last_source or "unknown",
            self._last_rssi if self._last_rssi is not None else "unknown",
        )
        self._async_notify_listeners()

    @callback
    def _async_notify_listeners_throttled(self) -> None:
        """Notify listeners at most once per ADVERTISEMENT_UPDATE_INTERVAL."""
        now = time.monotonic()
        if (
            self._last_listener_notify is not None
            and now - self._last_listener_notify < ADVERTISEMENT_UPDATE_INTERVAL
        ):
            return
        self._async_notify_listeners(now=now)

    @callback
    def _async_notify_listeners(self, *, now: float | None = None) -> None:
        """Notify listeners immediately, restarting the throttle window."""
        self._last_listener_notify = time.monotonic() if now is None else now
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:
                _LOGGER.warning("BLE availability listener error: %s", err)
