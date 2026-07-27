"""Tests for BLE availability telemetry.

Covers the advertisement telemetry helper, the unavailability tracker's
subscription lifecycle and throttling, and the correlation data attached to
connection attempts.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.adjustable_bed.adapter import AdapterSelectionResult
from custom_components.adjustable_bed.ble_availability import (
    ADVERTISEMENT_UPDATE_INTERVAL,
    POST_DISCONNECT_ADVERTISEMENT_GRACE,
    BleAvailabilityTracker,
    async_advertisement_telemetry,
)
from custom_components.adjustable_bed.const import (
    BED_TYPE_LINAK,
    CONF_BED_TYPE,
    CONF_DISABLE_ANGLE_SENSING,
    CONF_HAS_MASSAGE,
    CONF_MOTOR_COUNT,
    CONF_PREFERRED_ADAPTER,
    DOMAIN,
)
from custom_components.adjustable_bed.coordinator import AdjustableBedCoordinator

TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"

_LAST_SERVICE_INFO = (
    "custom_components.adjustable_bed.adapter.bluetooth.async_last_service_info"
)


def _service_info(
    *,
    age_seconds: float = 3.0,
    rssi: int = -71,
    source: str = "esp32-proxy",
) -> SimpleNamespace:
    """Return a scanner snapshot shaped like BluetoothServiceInfoBleak."""
    return SimpleNamespace(
        address=TEST_ADDRESS,
        name="Test Bed",
        rssi=rssi,
        source=source,
        connectable=True,
        time=time.monotonic() - age_seconds,
    )


class TestAdvertisementTelemetry:
    """Test the advertisement telemetry helper and its log formatting."""

    def test_reports_age_source_and_rssi(self):
        """Telemetry carries everything a failure line needs to be reconstructed."""
        with patch(_LAST_SERVICE_INFO, return_value=_service_info(age_seconds=4.0)):
            telemetry = async_advertisement_telemetry(MagicMock(), TEST_ADDRESS)

        assert telemetry.seen is True
        assert telemetry.age_seconds == pytest.approx(4.0, abs=0.5)
        assert telemetry.rssi == -71
        assert telemetry.source == "esp32-proxy"
        assert telemetry.connectable is True

        fragment = telemetry.as_log_fragment()
        assert "last advertisement" in fragment
        assert "esp32-proxy" in fragment
        assert "-71 dBm" in fragment
        assert "(connectable)" in fragment

    def test_dict_form_matches_log_form(self):
        """The diagnostics mapping exposes the same fields as the log fragment."""
        with patch(_LAST_SERVICE_INFO, return_value=_service_info()):
            telemetry = async_advertisement_telemetry(MagicMock(), TEST_ADDRESS)

        assert telemetry.as_dict() == {
            "seen": True,
            "age_seconds": telemetry.age_seconds,
            "rssi": -71,
            "source": "esp32-proxy",
            "connectable": True,
        }

    def test_missing_advertisement_is_reported_as_unseen(self):
        """A bed the scanner has never heard reports no advertisement."""
        with patch(_LAST_SERVICE_INFO, return_value=None):
            telemetry = async_advertisement_telemetry(MagicMock(), TEST_ADDRESS)

        assert telemetry.seen is False
        assert telemetry.age_seconds is None
        assert telemetry.as_log_fragment() == "no advertisement on record"

    def test_non_connectable_record_is_labelled(self):
        """A proxy that misclassifies the bed still proves it was advertising."""
        info = _service_info(source="proxy-nonconnectable")
        info.connectable = False
        with patch(_LAST_SERVICE_INFO, side_effect=[None, info]):
            telemetry = async_advertisement_telemetry(MagicMock(), TEST_ADDRESS)

        assert telemetry.seen is True
        assert telemetry.connectable is False
        assert "(non-connectable)" in telemetry.as_log_fragment()

    def test_bluetooth_errors_do_not_propagate(self):
        """Telemetry is best-effort: a Bluetooth stack error must not raise."""
        with patch(_LAST_SERVICE_INFO, side_effect=RuntimeError("manager not set")):
            telemetry = async_advertisement_telemetry(MagicMock(), TEST_ADDRESS)

        assert telemetry.seen is False
        assert telemetry.as_log_fragment() == "no advertisement on record"

    def test_missing_timestamp_reports_unknown_age(self):
        """A snapshot without a monotonic timestamp still reports RSSI and source."""
        info = _service_info()
        del info.time
        with patch(_LAST_SERVICE_INFO, return_value=info):
            telemetry = async_advertisement_telemetry(MagicMock(), TEST_ADDRESS)

        assert telemetry.seen is True
        assert telemetry.age_seconds is None
        assert telemetry.rssi == -71
        assert telemetry.as_log_fragment() == "no advertisement on record"


class TestBleAvailabilityTracker:
    """Test subscription lifecycle, transition logging, and throttling."""

    def _start(
        self,
        *,
        seed: SimpleNamespace | None = None,
    ) -> tuple[BleAvailabilityTracker, MagicMock, MagicMock, MagicMock, MagicMock]:
        """Start a tracker with stubbed Bluetooth subscriptions."""
        cancel_unavailable = MagicMock()
        cancel_advertisement = MagicMock()
        tracker = BleAvailabilityTracker(MagicMock(), TEST_ADDRESS)
        with (
            patch(_LAST_SERVICE_INFO, return_value=seed),
            patch(
                "custom_components.adjustable_bed.ble_availability.bluetooth.async_track_unavailable",
                return_value=cancel_unavailable,
            ) as track_unavailable,
            patch(
                "custom_components.adjustable_bed.ble_availability.bluetooth.async_register_callback",
                return_value=cancel_advertisement,
            ) as register_callback,
        ):
            tracker.async_start()
        return (
            tracker,
            track_unavailable,
            register_callback,
            cancel_unavailable,
            cancel_advertisement,
        )

    def test_start_registers_both_subscriptions(self):
        """Both the unavailable tracker and the advertisement callback register."""
        tracker, track_unavailable, register_callback, _, _ = self._start()

        assert tracker.tracking is True
        assert track_unavailable.call_count == 1
        assert track_unavailable.call_args.args[2] == TEST_ADDRESS
        assert track_unavailable.call_args.kwargs["connectable"] is True
        assert register_callback.call_args.args[2] == {
            "address": TEST_ADDRESS,
            "connectable": True,
        }

    def test_stop_unregisters_and_is_idempotent(self):
        """Stopping cancels both subscriptions exactly once."""
        tracker, _, _, cancel_unavailable, cancel_advertisement = self._start()

        tracker.async_stop()
        tracker.async_stop()

        assert cancel_unavailable.call_count == 1
        assert cancel_advertisement.call_count == 1
        assert tracker.tracking is False

    def test_start_returns_the_stop_callback(self):
        """The start callback returns the unsubscribe handle callers store."""
        cancel_unavailable = MagicMock()
        tracker = BleAvailabilityTracker(MagicMock(), TEST_ADDRESS)
        with (
            patch(_LAST_SERVICE_INFO, return_value=None),
            patch(
                "custom_components.adjustable_bed.ble_availability.bluetooth.async_track_unavailable",
                return_value=cancel_unavailable,
            ),
            patch(
                "custom_components.adjustable_bed.ble_availability.bluetooth.async_register_callback",
                return_value=MagicMock(),
            ),
        ):
            stop = tracker.async_start()

        stop()
        assert cancel_unavailable.call_count == 1
        assert tracker.tracking is False

    def test_failed_registration_unwinds(self):
        """A half-registered tracker cancels what it already registered."""
        cancel_unavailable = MagicMock()
        tracker = BleAvailabilityTracker(MagicMock(), TEST_ADDRESS)
        with (
            patch(_LAST_SERVICE_INFO, return_value=None),
            patch(
                "custom_components.adjustable_bed.ble_availability.bluetooth.async_track_unavailable",
                return_value=cancel_unavailable,
            ),
            patch(
                "custom_components.adjustable_bed.ble_availability.bluetooth.async_register_callback",
                side_effect=RuntimeError("BluetoothManager has not been set"),
            ),
        ):
            tracker.async_start()

        assert cancel_unavailable.call_count == 1
        assert tracker.tracking is False

    def test_start_seeds_state_from_scanner_history(self):
        """Entities get a value immediately from the existing scanner record."""
        tracker, _, _, _, _ = self._start(seed=_service_info(age_seconds=2.0))

        assert tracker.last_rssi == -71
        assert tracker.last_source == "esp32-proxy"
        assert tracker.last_advertisement is not None

    def test_unavailable_then_seen_again_transitions(self):
        """Both transitions are recorded and notified without throttling."""
        tracker, track_unavailable, register_callback, _, _ = self._start()
        updates: list[None] = []
        tracker.async_add_listener(lambda: updates.append(None))

        on_unavailable = track_unavailable.call_args.args[1]
        on_advertisement = register_callback.call_args.args[1]

        on_unavailable(_service_info())
        assert tracker.unavailable is True
        assert len(updates) == 1

        on_advertisement(_service_info(rssi=-80, source="hci0"), None)
        assert tracker.unavailable is False
        assert tracker.last_rssi == -80
        assert tracker.last_source == "hci0"
        assert len(updates) == 2

        diagnostics = tracker.diagnostics
        assert diagnostics["unavailable"] is False
        assert diagnostics["unavailable_transitions"] == 1
        assert diagnostics["last_unavailable_at"] is not None
        assert diagnostics["last_available_at"] is not None

    def test_repeated_unavailable_counts_once(self):
        """A repeated unavailable callback is not a new transition."""
        tracker, track_unavailable, _, _, _ = self._start()
        on_unavailable = track_unavailable.call_args.args[1]

        on_unavailable(_service_info())
        on_unavailable(_service_info())

        assert tracker.diagnostics["unavailable_transitions"] == 1

    def test_advertisement_updates_are_throttled(self):
        """Advertisements notify listeners at most once per throttle interval."""
        tracker, _, register_callback, _, _ = self._start()
        updates: list[None] = []
        tracker.async_add_listener(lambda: updates.append(None))
        on_advertisement = register_callback.call_args.args[1]

        for _ in range(20):
            on_advertisement(_service_info(), None)

        assert len(updates) == 1
        # Every advertisement still refreshes the recorded state; only the entity
        # writes are throttled.
        assert tracker.last_rssi == -71

        with patch(
            "custom_components.adjustable_bed.ble_availability.time.monotonic",
            return_value=time.monotonic() + ADVERTISEMENT_UPDATE_INTERVAL + 1,
        ):
            on_advertisement(_service_info(), None)

        assert len(updates) == 2

    def test_listener_can_unregister(self):
        """The unregister callback stops further notifications."""
        tracker, _, register_callback, _, _ = self._start()
        updates: list[None] = []
        unregister = tracker.async_add_listener(lambda: updates.append(None))
        on_advertisement = register_callback.call_args.args[1]

        unregister()
        on_advertisement(_service_info(), None)

        assert updates == []

    def test_listener_errors_do_not_break_other_listeners(self):
        """One misbehaving listener must not suppress the rest."""
        tracker, track_unavailable, _, _, _ = self._start()
        updates: list[None] = []

        def _raise() -> None:
            raise ValueError("listener boom")

        tracker.async_add_listener(_raise)
        tracker.async_add_listener(lambda: updates.append(None))
        track_unavailable.call_args.args[1](_service_info())

        assert updates == [None]

    def test_unavailable_while_connected_is_suppressed(self, caplog):
        """Advertisement absence during our own connection is expected, not a problem."""
        tracker, track_unavailable, _, _, _ = self._start()
        updates: list[None] = []
        tracker.async_add_listener(lambda: updates.append(None))
        on_unavailable = track_unavailable.call_args.args[1]

        tracker.async_set_connected(True)
        on_unavailable(_service_info())

        assert tracker.unavailable is False
        assert tracker.diagnostics["unavailable_transitions"] == 0
        assert "no longer sees advertisements" not in caplog.text
        assert updates == []

    def test_disconnect_with_stale_adverts_waits_for_grace(self, caplog):
        """After disconnect the bed gets the full grace window to resume advertising."""
        tracker, track_unavailable, _, _, _ = self._start()
        on_unavailable = track_unavailable.call_args.args[1]

        tracker.async_set_connected(True)
        on_unavailable(_service_info())
        with patch(
            "custom_components.adjustable_bed.ble_availability.async_call_later"
        ) as call_later:
            tracker.async_set_connected(False)

        assert call_later.call_count == 1
        assert call_later.call_args.args[1] == POST_DISCONNECT_ADVERTISEMENT_GRACE
        assert tracker.unavailable is False
        assert "no longer sees advertisements" not in caplog.text

    def test_grace_expiry_without_adverts_warns_once(self, caplog):
        """A bed still silent after the grace window is declared unavailable once."""
        tracker, track_unavailable, _, _, _ = self._start()
        on_unavailable = track_unavailable.call_args.args[1]

        tracker.async_set_connected(True)
        on_unavailable(_service_info())
        with patch(
            "custom_components.adjustable_bed.ble_availability.async_call_later"
        ) as call_later:
            tracker.async_set_connected(False)
        grace_expired = call_later.call_args.args[2]

        grace_expired(None)

        assert tracker.unavailable is True
        assert caplog.text.count("no longer sees advertisements") == 1

        # A repeated stack callback after the judgement is not a new transition.
        on_unavailable(_service_info())
        assert tracker.diagnostics["unavailable_transitions"] == 1
        assert caplog.text.count("no longer sees advertisements") == 1

    def test_advertisement_during_grace_cancels_judgement(self, caplog):
        """A bed that resumes advertising within the grace window stays available."""
        tracker, track_unavailable, register_callback, _, _ = self._start()
        on_unavailable = track_unavailable.call_args.args[1]
        on_advertisement = register_callback.call_args.args[1]

        tracker.async_set_connected(True)
        on_unavailable(_service_info())
        with patch(
            "custom_components.adjustable_bed.ble_availability.async_call_later"
        ) as call_later:
            tracker.async_set_connected(False)
            on_advertisement(_service_info(), None)

        assert call_later.return_value.call_count == 1  # timer cancelled
        # Even if the timer had fired anyway, adverts are fresh: no judgement.
        call_later.call_args.args[2](None)
        assert tracker.unavailable is False
        assert "no longer sees advertisements" not in caplog.text

    def test_unavailable_shortly_after_disconnect_defers_remaining_grace(self):
        """A stack callback inside the grace window schedules the remainder."""
        tracker, track_unavailable, _, _, _ = self._start()
        on_unavailable = track_unavailable.call_args.args[1]

        tracker.async_set_connected(True)
        tracker.async_set_connected(False)
        with patch(
            "custom_components.adjustable_bed.ble_availability.async_call_later"
        ) as call_later:
            on_unavailable(_service_info())

        assert tracker.unavailable is False
        assert call_later.call_count == 1
        assert 0 < call_later.call_args.args[1] <= POST_DISCONNECT_ADVERTISEMENT_GRACE

    def test_connecting_clears_declared_unavailable(self):
        """Holding the connection is proof of availability."""
        tracker, track_unavailable, _, _, _ = self._start()
        updates: list[None] = []
        tracker.async_add_listener(lambda: updates.append(None))
        on_unavailable = track_unavailable.call_args.args[1]

        on_unavailable(_service_info())  # never connected: declared immediately
        assert tracker.unavailable is True

        tracker.async_set_connected(True)

        assert tracker.unavailable is False
        assert tracker.diagnostics["last_available_at"] is not None
        assert len(updates) == 2

    def test_diagnostics_report_untracked_state(self):
        """Diagnostics stay readable when tracking never started."""
        tracker = BleAvailabilityTracker(MagicMock(), TEST_ADDRESS)

        with patch(_LAST_SERVICE_INFO, return_value=None):
            diagnostics = tracker.diagnostics

        assert diagnostics["tracking"] is False
        assert diagnostics["unavailable"] is False
        assert diagnostics["last_advertisement"] is None
        assert diagnostics["current"]["seen"] is False


class TestConnectionAttemptCorrelation:
    """Test that connection attempts record the bed's visibility."""

    async def test_failed_attempt_records_advertisement_telemetry(
        self,
        hass: HomeAssistant,
        mock_coordinator_connected: None,  # noqa: ARG002
    ):
        """A device-not-found attempt records what the scanner last heard."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Test Bed",
            data={
                CONF_ADDRESS: TEST_ADDRESS,
                CONF_NAME: "Test Bed",
                CONF_BED_TYPE: BED_TYPE_LINAK,
                CONF_MOTOR_COUNT: 2,
                CONF_HAS_MASSAGE: False,
                CONF_DISABLE_ANGLE_SENSING: True,
                CONF_PREFERRED_ADAPTER: "auto",
            },
            unique_id=TEST_ADDRESS,
            entry_id="ble_availability_entry",
        )
        entry.add_to_hass(hass)

        adapter_result = AdapterSelectionResult(
            device=None,
            source=None,
            rssi=None,
            connectable=None,
            available_sources=[],
        )
        with (
            patch(
                "custom_components.adjustable_bed.coordinator.select_adapter",
                new_callable=AsyncMock,
                return_value=adapter_result,
            ),
            patch(_LAST_SERVICE_INFO, return_value=_service_info(age_seconds=5.0)),
            patch(
                "custom_components.adjustable_bed.coordinator.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            coordinator = AdjustableBedCoordinator(hass, entry)
            coordinator._max_retries = 1
            result = await coordinator.async_connect()

        assert result is False
        attempt = coordinator.connection_attempt_details[0]
        assert attempt["result"] == "device_not_found"
        assert attempt["advertisement_at_start"]["source"] == "esp32-proxy"
        assert attempt["advertisement_at_failure"]["rssi"] == -71
        assert attempt["advertisement_at_failure"]["age_seconds"] == pytest.approx(5.0, abs=0.5)

