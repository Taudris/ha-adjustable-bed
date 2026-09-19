"""Best-effort Bluetooth evidence for failed connections and support reports."""

import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothReachabilityIntent
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def connection_reachability(hass: HomeAssistant, address: str) -> str | None:
    """Describe the current paths without masking the original connection error.

    The returned prose is for humans only, never a routing or retry input.
    Exporters must apply their normal report redaction to it.
    """
    try:
        return bluetooth.async_address_reachability_diagnostics(
            hass, address, BluetoothReachabilityIntent.CONNECTION
        )
    except Exception:  # A missing/unloading Bluetooth manager is valid here.
        _LOGGER.debug("Bluetooth reachability diagnostics unavailable", exc_info=True)
        return None
