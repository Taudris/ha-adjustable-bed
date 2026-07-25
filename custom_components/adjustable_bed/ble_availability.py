"""BLE availability telemetry for the Adjustable Bed integration.

Home Assistant's Bluetooth stack already knows when a bed last advertised, how
strong that advertisement was, and which adapter or proxy heard it. Connection
failures - especially ESP_GATT status 133 through ESPHome proxies - are hard to
interpret without that context: a bed that stopped advertising while another
central held it looks exactly like a bed that is simply out of range.

This module records that context and makes it available to connection logging,
diagnostic entities, and the diagnostics download. It is observation only:
nothing here gates, delays, or otherwise changes connection behaviour.
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

from .adapter import find_service_info_by_address

_LOGGER = logging.getLogger(__name__)

# Beds advertise several times per second. Entity state writes are throttled to
# this interval so the recorder does not fill with BLE noise; availability
# transitions bypass the throttle because those are the interesting events.
ADVERTISEMENT_UPDATE_INTERVAL = 30.0


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
        """Return True when Home Assistant has declared the bed gone."""
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
        while self._cancel_callbacks:
            self._cancel_callbacks.pop()()

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
        """Log that Home Assistant no longer sees the bed advertising."""
        del service_info  # The last-known advertisement is already recorded
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
