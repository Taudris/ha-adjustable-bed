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
The judgement itself is the pure judge_availability function; the tracker only
supplies its inputs and reacts to the verdict.

The verdict feeds logs and the diagnostics download. It deliberately does not
gate Home Assistant entity availability: this is an on-demand-connect device
whose entities must stay actionable so that a command can trigger a reconnect.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
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


class AvailabilityVerdict(StrEnum):
    """What the availability inputs say about the bed at one instant."""

    AVAILABLE = "available"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"


def judge_availability(
    *,
    connected: bool,
    stack_unavailable: bool,
    grace_remaining: float | None,
) -> AvailabilityVerdict:
    """Return the availability verdict for one set of inputs.

    Pure and total: the entire judgement is these three inputs, which is what
    makes it testable on its own and dumpable straight into diagnostics.

    Holding the connection, or the stack still hearing advertisements, means
    available. Silence inside the post-disconnect grace is deferred rather than
    decided, because the bed may still be resuming its advertising after we
    released it. Silence past the grace is the bed's own.
    """
    if connected or not stack_unavailable:
        return AvailabilityVerdict.AVAILABLE
    if grace_remaining is not None:
        return AvailabilityVerdict.DEFERRED
    return AvailabilityVerdict.UNAVAILABLE


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

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        *,
        is_connected: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the tracker for a configured address.

        ``is_connected`` is read at decision time to answer "do we hold this
        bed's connection right now". It is a pull rather than a pushed cache on
        purpose: a cache would need "every connect notification is eventually
        followed by a disconnect notification" to hold across every call site,
        and a single missed disconnect would pin the verdict to available
        forever. Omitting it means the tracker judges as if never connected.

        ``clock`` is this tracker's own monotonic source, used for the grace
        window and the listener throttle. It is deliberately *not* used for
        advertisement ages: those are differences against the Bluetooth stack's
        timestamps and must stay on the real monotonic clock.
        """
        self.hass = hass
        self._address = address.upper()
        self._is_connected = is_connected
        self._clock = clock if clock is not None else time.monotonic
        self._cancel_callbacks: list[Callable[[], None]] = []
        self._listeners: set[Callable[[], None]] = set()
        self._unavailable = False
        # Edge detection for the disconnect stamp only. Never a decision input:
        # see _connected_now.
        self._connection_observed = False
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
        stayed absent past the post-disconnect grace. This is the last verdict
        that was logged and published to listeners, so it changes only on the
        events that produce those - see judge_availability for the judgement
        itself, and the "decision" block in diagnostics for its live inputs.
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
    def async_connection_state_changed(self, connected: bool) -> None:
        """Re-read the connection state after the coordinator reports a change.

        The pushed value is a wake-up, not the state: the coordinator also
        sends False at the *start* of a connect attempt so the connectivity
        sensor can show a connecting state. Taking that at face value would
        stamp a disconnect that never happened and re-arm the grace window on
        every attempt, deferring the verdict exactly when repeated connect
        failures make it most worth having. Reading the coordinator instead
        makes the tracker see only real edges.
        """
        del connected  # A hint that state may have changed, not the state
        self._async_sync_connection_state()

    @callback
    def _async_sync_connection_state(self) -> None:
        """Act on a change in whether we hold the bed's connection.

        The bed stops advertising while connected, so the tracker must know
        when advertisement absence is our own doing: while connected it is
        expected, and after we disconnect the bed gets
        POST_DISCONNECT_ADVERTISEMENT_GRACE to resume advertising before
        absence counts against it.
        """
        connected = self._connected_now()
        if connected == self._connection_observed:
            return
        self._connection_observed = connected

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

        self._last_disconnect_monotonic = self._clock()
        # The stack may have stopped seeing advertisements while we held the
        # connection (expected, and suppressed at the time). Re-judge now that
        # the grace window has started.
        self._async_reevaluate()

    def _connected_now(self) -> bool:
        """Return whether this integration holds the bed's connection right now.

        Best-effort like the rest of the module: an unreadable coordinator
        counts as not connected, which errs toward reporting a problem rather
        than hiding one.
        """
        if self._is_connected is None:
            return False
        try:
            return bool(self._is_connected())
        except Exception as err:
            _LOGGER.debug("Could not read connection state for %s: %s", self._address, err)
            return False

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
            "connected": self._connected_now(),
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

        A bed the stack has no record of at all is judged here and now, because
        the stack will never judge it for us: it derives disappearances from
        history minus discovered, so an address that was never in history can
        never disappear and no unavailable callback will ever arrive. Without
        this, a bed powered off before Home Assistant started would be reported
        available forever. Judging early is safe - the verdict only feeds logs
        and diagnostics, and the first advertisement clears it.
        """
        telemetry = self.telemetry
        if not telemetry.seen:
            self._stack_unavailable = True
            self._async_reevaluate()
            return
        if telemetry.age_seconds is None:
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
        self._async_reevaluate()

    @callback
    def _async_reevaluate(self) -> None:
        """Re-judge availability and act on the verdict.

        Every path that can change one of the three judgement inputs ends here,
        so the verdict cannot diverge between the stack callback, the grace
        timer, and our own connection state.
        """
        if self._unavailable:
            return

        connected = self._connected_now()
        grace_remaining = self._grace_remaining()
        verdict = judge_availability(
            connected=connected,
            stack_unavailable=self._stack_unavailable,
            grace_remaining=grace_remaining,
        )

        if verdict is AvailabilityVerdict.UNAVAILABLE:
            self._async_declare_unavailable()
        elif verdict is AvailabilityVerdict.DEFERRED and grace_remaining is not None:
            # We disconnected moments ago; give the bed the rest of the grace
            # window to resume advertising before judging it gone.
            self._async_schedule_grace_check(grace_remaining)
        elif connected and self._stack_unavailable:
            # Expected: the bed cannot advertise while we hold its connection.
            _LOGGER.debug(
                "Bed %s stopped advertising while this integration holds its "
                "connection; expected, not an availability problem",
                self._address,
            )

    def _grace_remaining(self) -> float | None:
        """Return seconds left in the post-disconnect grace window, if any."""
        if self._last_disconnect_monotonic is None:
            return None
        elapsed = self._clock() - self._last_disconnect_monotonic
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
        """Re-judge once the post-disconnect grace window has elapsed.

        The timer runs on the event loop's monotonic clock, the same one the
        grace window is measured against, so by the time this fires
        _grace_remaining() has already gone to None.
        """
        self._cancel_grace = None
        self._async_reevaluate()

    @callback
    def _async_declare_unavailable(self) -> None:
        """Record and log that the bed is genuinely unavailable, once."""
        if self._unavailable:
            return

        self._unavailable = True
        self._unavailable_transitions += 1
        self._last_unavailable_at = datetime.now(UTC)
        if self._last_advertisement is None:
            _LOGGER.info(
                "Home Assistant has never heard an advertisement from bed %s. The bed "
                "may be powered off, out of range, or holding a connection to another "
                "device",
                self._address,
            )
        else:
            _LOGGER.info(
                "Home Assistant no longer sees advertisements from bed %s "
                "(last advertisement %s via %s at %s dBm). The bed may be powered off, out of "
                "range, or holding a connection to another device",
                self._address,
                self._last_advertisement.isoformat(),
                self._last_source or "unknown",
                self._last_rssi if self._last_rssi is not None else "unknown",
            )
        self._async_notify_listeners()

    @callback
    def _async_notify_listeners_throttled(self) -> None:
        """Notify listeners at most once per ADVERTISEMENT_UPDATE_INTERVAL."""
        now = self._clock()
        if (
            self._last_listener_notify is not None
            and now - self._last_listener_notify < ADVERTISEMENT_UPDATE_INTERVAL
        ):
            return
        self._async_notify_listeners(now=now)

    @callback
    def _async_notify_listeners(self, *, now: float | None = None) -> None:
        """Notify listeners immediately, restarting the throttle window."""
        self._last_listener_notify = self._clock() if now is None else now
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:
                _LOGGER.warning("BLE availability listener error: %s", err)
