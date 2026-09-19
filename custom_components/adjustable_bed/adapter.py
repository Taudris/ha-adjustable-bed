"""Bluetooth adapter selection and BLE helper functions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.core import HomeAssistant

from .bluetooth_transport import async_connection_paths
from .const import (
    ADAPTER_AUTO,
    DEVICE_INFO_CHARS,
    DEVICE_INFO_READ_TIMEOUT,
    DEVICE_INFO_SERVICE_UUID,
)

# Sentinel value indicating RSSI is unavailable
RSSI_UNAVAILABLE = -999

_LOGGER = logging.getLogger(__name__)


@dataclass
class AdapterSelectionResult:
    """Result from adapter selection process."""

    device: BLEDevice | None
    source: str | None
    rssi: int | None
    connectable: bool | None
    available_sources: list[str]


def get_discovered_service_info(
    hass: HomeAssistant,
    *,
    include_non_connectable: bool = False,
) -> list[BluetoothServiceInfoBleak]:
    """Return discovered BLE service info, optionally including non-connectable records.

    Home Assistant keeps separate snapshots for connectable and non-connectable
    advertisements. Some proxies have been observed to classify connectable beds
    as non-connectable, so callers can opt into a fallback merge when the strict
    connectable view is empty.
    """

    connectable_states = (True, False) if include_non_connectable else (True,)
    discovered: list[BluetoothServiceInfoBleak] = []
    seen: set[tuple[str, str | None]] = set()

    for connectable in connectable_states:
        for service_info in bluetooth.async_discovered_service_info(
            hass,
            connectable=connectable,
        ):
            key = (
                service_info.address.upper(),
                getattr(service_info, "source", None),
            )
            if key in seen:
                continue
            seen.add(key)
            discovered.append(service_info)

    return discovered


def find_service_info_by_address(
    hass: HomeAssistant,
    address: str,
    *,
    allow_non_connectable: bool = False,
) -> tuple[BluetoothServiceInfoBleak | None, bool]:
    """Return the latest service info snapshot for an address.

    The boolean in the tuple indicates whether the matching snapshot came from
    the connectable scanner view.
    """

    normalized_address = address.upper()
    connectable_states = (True, False) if allow_non_connectable else (True,)

    for connectable in connectable_states:
        service_info = bluetooth.async_last_service_info(
            hass,
            normalized_address,
            connectable=connectable,
        )
        if service_info is not None and service_info.address.upper() == normalized_address:
            return service_info, connectable

    if allow_non_connectable:
        for service_info in get_discovered_service_info(
            hass,
            include_non_connectable=True,
        ):
            if service_info.address.upper() == normalized_address:
                return service_info, bool(getattr(service_info, "connectable", False))

    return None, False


def get_service_info_snapshots_by_address(
    hass: HomeAssistant,
    address: str,
    *,
    allow_non_connectable: bool = False,
) -> list[tuple[BluetoothServiceInfoBleak, bool]]:
    """Return all current scanner snapshots for an address."""
    normalized_address = address.upper()
    snapshots: list[tuple[BluetoothServiceInfoBleak, bool]] = []

    for service_info in get_discovered_service_info(
        hass,
        include_non_connectable=allow_non_connectable,
    ):
        if service_info.address.upper() != normalized_address:
            continue
        connectable = bool(getattr(service_info, "connectable", True))
        snapshots.append((service_info, connectable))

    snapshots.sort(
        key=lambda item: (
            item[1],
            getattr(item[0], "rssi", RSSI_UNAVAILABLE),
        ),
        reverse=True,
    )
    return snapshots


def get_ble_device_with_fallback(
    hass: HomeAssistant,
    address: str,
    *,
    allow_non_connectable: bool = False,
) -> tuple[BLEDevice | None, bool]:
    """Return a BLEDevice for an address, falling back to non-connectable state."""

    normalized_address = address.upper()
    device = bluetooth.async_ble_device_from_address(
        hass,
        normalized_address,
        connectable=True,
    )
    if device is not None:
        return device, True

    if not allow_non_connectable:
        return None, False

    service_info, connectable = find_service_info_by_address(
        hass,
        normalized_address,
        allow_non_connectable=True,
    )
    if service_info is not None and getattr(service_info, "device", None) is not None:
        return service_info.device, connectable

    device = bluetooth.async_ble_device_from_address(
        hass,
        normalized_address,
        connectable=False,
    )
    if device is not None:
        return device, False

    return None, False


async def select_adapter(
    hass: HomeAssistant,
    address: str,
    preferred_adapter: str | None,
    exclude_adapters: set[str] | None = None,
) -> AdapterSelectionResult:
    """Choose an observed path, retaining HA's ranking for automatic selection.

    A selected BLEDevice is a routing hint: HA's wrapper may choose a different
    backend during connect. The coordinator records the actual source afterward.
    """
    paths = async_connection_paths(hass, address)
    available = [f"{path.source} (RSSI: {path.rssi})" for path in paths]
    candidates = [path for path in paths if path.source not in (exclude_adapters or ())]
    candidates.sort(key=lambda path: not path.can_connect)
    if preferred_adapter and preferred_adapter != ADAPTER_AUTO:
        candidates.sort(key=lambda path: (not path.can_connect, path.source != preferred_adapter))
    for path in candidates:
        # Resolve from the chosen scanner only. A generic fallback here could
        # silently hand us the excluded/exhausted adapter again.
        try:
            scanner_devices = bluetooth.async_scanner_devices_by_address(
                hass, address.upper(), connectable=path.connectable
            )
        except (KeyError, RuntimeError):
            _LOGGER.debug("Scanner disappeared while selecting %s", path.source)
            continue
        for scanner_device in scanner_devices:
            if scanner_device.scanner.source == path.source:
                return AdapterSelectionResult(
                    scanner_device.ble_device, path.source, path.rssi,
                    path.connectable, available,
                )
    if exclude_adapters:
        return AdapterSelectionResult(None, None, None, None, available)
    device, connectable = get_ble_device_with_fallback(
        hass, address, allow_non_connectable=True
    )
    source = device.details.get("source") if device and isinstance(device.details, dict) else None
    return AdapterSelectionResult(device, source, None, connectable, available)


def detect_esphome_proxy(hass: HomeAssistant, address: str) -> bool:
    """Detect if connection is through an ESPHome Bluetooth proxy.

    Args:
        hass: Home Assistant instance
        address: The BLE device address

    Returns:
        True if ESPHome proxy detected, False otherwise
    """
    try:
        service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
        if service_info:
            _LOGGER.debug(
                "Service info: source=%s, rssi=%s, connectable=%s, service_uuids=%s",
                getattr(service_info, "source", "N/A"),
                getattr(service_info, "rssi", "N/A"),
                getattr(service_info, "connectable", "N/A"),
                getattr(service_info, "service_uuids", []),
            )
            # Check if this is from an ESPHome proxy
            info_source = getattr(service_info, "source", "")
            if info_source and "esphome" in info_source.lower():
                _LOGGER.info(
                    "Device discovered via ESPHome Bluetooth proxy: %s",
                    info_source,
                )
                return True
    except Exception as err:
        _LOGGER.debug("Could not get detailed service info: %s", err)

    return False


async def discover_services(client: BleakClient, address: str) -> bool:
    """Explicitly discover BLE services and log the hierarchy.

    Some backends don't auto-discover services, so this ensures
    services are available before we try to use them.

    Args:
        client: The connected BleakClient
        address: The BLE device address (for logging)

    Returns:
        True if services were discovered successfully, False otherwise
    """
    # Access client.services to trigger service discovery
    # In modern Bleak, services are auto-discovered when accessed
    _LOGGER.debug("Discovering BLE services...")
    try:
        # Accessing .services triggers discovery if not already done
        _ = client.services
    except Exception as err:
        _LOGGER.warning("Failed to discover services on %s: %s", address, err)
        # Continue anyway - services might already be populated

    # Log discovered services in detail
    if client.services:
        services_list = list(client.services)
        _LOGGER.debug(
            "Discovered %d BLE services on %s:",
            len(services_list),
            address,
        )
        for service in client.services:
            _LOGGER.debug(
                "  Service: %s (handle: %s)",
                service.uuid,
                getattr(service, "handle", "N/A"),
            )
            for char in service.characteristics:
                props = ", ".join(char.properties)
                _LOGGER.debug(
                    "    Characteristic: %s [%s] (handle: %s)",
                    char.uuid,
                    props,
                    getattr(char, "handle", "N/A"),
                )
                for desc in char.descriptors:
                    _LOGGER.debug(
                        "      Descriptor: %s (handle: %s)",
                        desc.uuid,
                        getattr(desc, "handle", "N/A"),
                    )
        return True
    else:
        _LOGGER.warning(
            "No BLE services discovered on %s - this may indicate a connection issue",
            address,
        )
        return False


async def read_ble_device_info(client: BleakClient, address: str) -> tuple[str | None, str | None]:
    """Read manufacturer and model from BLE Device Information Service.

    This reads the standard BLE Device Information Service (UUID 0x180A)
    if available.

    Args:
        client: The connected BleakClient
        address: The BLE device address (for logging)

    Returns:
        Tuple of (manufacturer, model), either can be None if not available
    """
    manufacturer: str | None = None
    model: str | None = None

    if not client.is_connected:
        return manufacturer, model

    if not client.services:
        return manufacturer, model

    # Check if Device Information Service exists
    has_device_info = False
    for service in client.services:
        if service.uuid.lower() == DEVICE_INFO_SERVICE_UUID:
            has_device_info = True
            break

    if not has_device_info:
        _LOGGER.debug("Device Information Service not found on %s", address)
        return manufacturer, model

    _LOGGER.debug("Reading Device Information Service from %s", address)

    # Read manufacturer name
    try:
        manufacturer_uuid = DEVICE_INFO_CHARS["manufacturer_name"]
        value = await asyncio.wait_for(
            client.read_gatt_char(manufacturer_uuid), DEVICE_INFO_READ_TIMEOUT
        )
        try:
            manufacturer = value.decode("utf-8").rstrip("\x00")
            _LOGGER.debug("BLE manufacturer: %s", manufacturer)
        except UnicodeDecodeError:
            _LOGGER.debug("Could not decode manufacturer name as UTF-8")
    except (BleakError, TimeoutError, OSError) as err:
        _LOGGER.debug("Could not read manufacturer name: %s", err)

    # Read model number
    try:
        model_uuid = DEVICE_INFO_CHARS["model_number"]
        value = await asyncio.wait_for(
            client.read_gatt_char(model_uuid), DEVICE_INFO_READ_TIMEOUT
        )
        try:
            model = value.decode("utf-8").rstrip("\x00")
            _LOGGER.debug("BLE model: %s", model)
        except UnicodeDecodeError:
            _LOGGER.debug("Could not decode model number as UTF-8")
    except (BleakError, TimeoutError, OSError) as err:
        _LOGGER.debug("Could not read model number: %s", err)

    # WLT QRRM controllers do not expose the standard Model Number
    # characteristic. Instead, they expose two Software Revision (0x2A28)
    # characteristics: a firmware version followed by a vendor model such as
    # ``WLT825X_H35``. Reading 0x2A28 by UUID only returns the first instance,
    # so inspect the characteristic objects to recover the model from the
    # duplicate instance. This model distinguishes otherwise identical BedTech
    # and Richmat/Casper QRRM controllers (issues #410 and #300).
    if not (model or "").strip() and (manufacturer or "").strip().casefold() == "wlt":
        software_revision_uuid = DEVICE_INFO_CHARS["software_revision"]
        software_revision_chars = [
            characteristic
            for service in client.services
            if service.uuid.lower() == DEVICE_INFO_SERVICE_UUID
            for characteristic in service.characteristics
            if characteristic.uuid.lower() == software_revision_uuid
        ]
        if len(software_revision_chars) > 1:
            for characteristic in software_revision_chars:
                try:
                    value = await asyncio.wait_for(
                        client.read_gatt_char(characteristic),
                        DEVICE_INFO_READ_TIMEOUT,
                    )
                    candidate = value.decode("utf-8").rstrip("\x00")
                except (BleakError, TimeoutError, OSError, UnicodeDecodeError) as err:
                    _LOGGER.debug(
                        "Could not read WLT vendor model from handle %s: %s",
                        getattr(characteristic, "handle", "unknown"),
                        err,
                    )
                    continue

                if candidate.upper().startswith("WLT"):
                    model = candidate
                    _LOGGER.debug("BLE vendor model: %s", model)
                    break

    return manufacturer, model
