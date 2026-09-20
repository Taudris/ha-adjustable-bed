"""Tests for the bundled Adjustable Bed Lovelace card registration."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.lovelace import LovelaceData
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.const import (
    CONF_ID,
    CONF_TYPE,
    CONF_URL,
    EVENT_COMPONENT_LOADED,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.adjustable_bed.const import DOMAIN
from custom_components.adjustable_bed.frontend import (
    CARD_FILENAME,
    CARD_FRESHNESS_COMMAND,
    CARD_URL,
    CHUNK_FILENAME,
    DATA_FRONTEND_REGISTERED,
    URL_BASE,
    _async_register_lovelace_resource,
    _card_url,
    _chunk_url,
    _dist_dir,
    _gather,
    async_register_frontend,
)


def _expected_static_paths(cache_key: str) -> list[tuple[str, str, bool]]:
    """The two exact-path routes, as (url_path, file, cache_headers) triples."""
    return [
        (_card_url(cache_key), str(_dist_dir() / CARD_FILENAME), True),
        (_chunk_url(cache_key), str(_dist_dir() / CHUNK_FILENAME), True),
    ]


def _registered_static_paths(hass: HomeAssistant) -> list[tuple[str, str, bool]]:
    """Read back the triples of the one registration call."""
    configs = hass.http.async_register_static_paths.await_args.args[0]
    return [
        (config.url_path, config.path, config.cache_headers) for config in configs
    ]


def _storage_resources(items: list[dict[str, str]]) -> MagicMock:
    """Return a storage resource collection mock with the given items."""
    resources = MagicMock(spec=ResourceStorageCollection)
    resources.async_get_info = AsyncMock(return_value={"resources": len(items)})
    resources.async_items.return_value = items
    resources.async_create_item = AsyncMock()
    resources.async_delete_item = AsyncMock()
    resources.async_update_item = AsyncMock()
    return resources


def _dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the integration at a scratch dist directory holding both files."""
    (tmp_path / CARD_FILENAME).write_bytes(b"first entry")
    (tmp_path / CHUNK_FILENAME).write_bytes(b"chunk")
    monkeypatch.setattr(
        "custom_components.adjustable_bed.frontend._dist_dir",
        lambda: tmp_path,
    )
    return tmp_path


def test_gather_uses_bundle_digest_in_cache_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Card changes invalidate the browser cache without a version bump.

    Covers `entry-url-per-request`.
    """
    dist = _dist(tmp_path, monkeypatch)

    exists, version, first_cache_key = _gather()
    (dist / CARD_FILENAME).write_bytes(b"changed entry")
    _, changed_version, entry_cache_key = _gather()
    (dist / CHUNK_FILENAME).write_bytes(b"changed chunk")
    _, _, chunk_cache_key = _gather()

    assert exists
    assert changed_version == version
    assert first_cache_key.startswith(f"{version}-")
    assert entry_cache_key.startswith(f"{version}-")
    # Either shipped file moving the key is what keeps the pair consistent.
    assert len({first_cache_key, entry_cache_key, chunk_cache_key}) == 3


def test_gather_requires_the_entry_and_the_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-built bundle is reported missing, not served.

    Covers `missing-file-warning-covers-chunk`.
    """
    dist = _dist(tmp_path, monkeypatch)

    (dist / CHUNK_FILENAME).unlink()
    assert not _gather()[0]

    (dist / CHUNK_FILENAME).write_bytes(b"chunk")
    (dist / CARD_FILENAME).unlink()
    assert not _gather()[0]


def test_card_url_embeds_cache_key_in_path() -> None:
    """Bundle identity does not depend on cache-sensitive query parameters.

    Covers `entry-url-per-request`.
    """
    assert _card_url("3.5.0-abc123") == (
        f"{URL_BASE}/3.5.0-abc123/{CARD_FILENAME}"
    )
    assert _chunk_url("3.5.0-abc123") == (
        f"{URL_BASE}/3.5.0-abc123/{CHUNK_FILENAME}"
    )


def test_committed_entry_imports_the_filename_the_chunk_route_serves() -> None:
    """The specifier the browser requests is the one the route publishes.

    The name lives in the build and in this module, so renaming it on one
    side only fails here rather than in a browser.
    """
    entry = (_dist_dir() / CARD_FILENAME).read_text(encoding="utf-8")

    assert re.findall(r'import\(\s*"([^"]+)"\s*\)', entry) == [
        f"./{CHUNK_FILENAME}"
    ]


async def test_register_lovelace_resource_creates_missing_resource(
    hass: HomeAssistant,
) -> None:
    """The card is persisted so Lovelace can load it independently.

    Covers `storage-resource-mirrors-injection`.
    """
    resources = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {}, resources, {})
    card_url = _card_url("3.5.0-abc123")

    assert await _async_register_lovelace_resource(hass, card_url)

    assert [
        {CONF_TYPE: item[CONF_TYPE], CONF_URL: item[CONF_URL]}
        for item in resources.async_items()
    ] == [{CONF_TYPE: "module", CONF_URL: card_url}]


async def test_register_lovelace_resource_updates_stale_resource(
    hass: HomeAssistant,
) -> None:
    """Lazy-loaded storage migrates the query-based card resource.

    Covers `storage-resource-mirrors-injection`.
    """
    resources = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    await resources.store.async_save(
        {
            "items": [
                {
                    CONF_ID: "resource-id",
                    CONF_TYPE: "js",
                    CONF_URL: f"{CARD_URL}?v=3.2.1",
                },
                {
                    CONF_ID: "other-resource",
                    CONF_TYPE: "module",
                    CONF_URL: "/local/unrelated-card.js",
                },
            ]
        }
    )
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {}, resources, {})
    assert not resources.loaded
    card_url = _card_url("3.5.0-def456")

    assert await _async_register_lovelace_resource(hass, card_url)

    assert resources.loaded
    assert sorted(resources.async_items(), key=lambda item: item[CONF_ID]) == [
        {
            CONF_ID: "other-resource",
            CONF_TYPE: "module",
            CONF_URL: "/local/unrelated-card.js",
        },
        {
            CONF_ID: "resource-id",
            CONF_TYPE: "module",
            CONF_URL: card_url,
        },
    ]


async def test_register_lovelace_resource_leaves_current_resource_unchanged(
    hass: HomeAssistant,
) -> None:
    """Repeated setup is idempotent.

    Covers `storage-resource-mirrors-injection`.
    """
    card_url = _card_url("3.5.0-abc123")
    resources = _storage_resources(
        [
            {
                CONF_ID: "resource-id",
                CONF_TYPE: "module",
                CONF_URL: card_url,
            }
        ]
    )
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {}, resources, {})

    assert await _async_register_lovelace_resource(hass, card_url)

    resources.async_create_item.assert_not_awaited()
    resources.async_delete_item.assert_not_awaited()
    resources.async_update_item.assert_not_awaited()


async def test_register_lovelace_resource_removes_duplicate_card_resources(
    hass: HomeAssistant,
) -> None:
    """Only one integration-owned card resource survives reconciliation.

    Covers `storage-resource-mirrors-injection`.
    """
    card_url = _card_url("3.5.0-abc123")
    resources = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    await resources.store.async_save(
        {
            "items": [
                {
                    CONF_ID: "legacy-resource",
                    CONF_TYPE: "module",
                    CONF_URL: f"{CARD_URL}?v=3.4.0-old",
                },
                {
                    CONF_ID: "current-resource",
                    CONF_TYPE: "module",
                    CONF_URL: card_url,
                },
            ]
        }
    )
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {}, resources, {})

    assert await _async_register_lovelace_resource(hass, card_url)

    assert resources.async_items() == [
        {
            CONF_ID: "current-resource",
            CONF_TYPE: "module",
            CONF_URL: card_url,
        }
    ]


async def test_register_lovelace_resource_rejects_yaml_resources(
    hass: HomeAssistant,
) -> None:
    """YAML resource collections are immutable and use the module fallback.

    Covers `non-storage-writes-nothing`.
    """
    hass.data[LOVELACE_DATA] = LovelaceData(
        "yaml",
        {},
        ResourceYAMLCollection([]),
        {},
    )

    assert not await _async_register_lovelace_resource(
        hass,
        _card_url("3.5.0-abc123"),
    )


async def test_register_frontend_uses_resource_and_module_hook(
    hass: HomeAssistant,
) -> None:
    """Storage mode is durable while retaining early frontend module loading.

    Covers `bundle-responses-cacheable`, `entry-url-per-request`,
    `publish-once-per-run` and `storage-resource-mirrors-injection`.
    """
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.update({"frontend", "lovelace"})

    with (
        patch(
            "custom_components.adjustable_bed.frontend._gather",
            return_value=(True, "3.3.0", "3.3.0-abc123"),
        ),
        patch(
            "custom_components.adjustable_bed.frontend._async_register_lovelace_resource",
            new_callable=AsyncMock,
            return_value=True,
        ) as register_resource,
        patch(
            "custom_components.adjustable_bed.frontend.add_extra_js_url"
        ) as add_extra_js_url,
        patch(
            "custom_components.adjustable_bed.frontend.websocket_api.async_register_command"
        ) as register_command,
    ):
        await async_register_frontend(hass)
        await async_register_frontend(hass)
        await hass.async_block_till_done()

    register_resource.assert_awaited_once_with(hass, CARD_URL)
    add_extra_js_url.assert_called_once_with(hass, CARD_URL)
    register_command.assert_called_once()
    hass.http.async_register_static_paths.assert_awaited_once()
    assert _registered_static_paths(hass) == _expected_static_paths("3.3.0-abc123")
    hass.http.register_view.assert_called_once()
    assert hass.data[DOMAIN][DATA_FRONTEND_REGISTERED] is True


async def test_register_frontend_answers_the_freshness_command(
    hass: HomeAssistant,
) -> None:
    """An open page can ask which cache key this run serves.

    Covers `freshness-checked-on-connect`.
    """
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    connection = MagicMock(spec=websocket_api.ActiveConnection)
    connection.user = MagicMock(is_admin=False)

    with patch(
        "custom_components.adjustable_bed.frontend._gather",
        return_value=(True, "3.3.0", "3.3.0-abc123"),
    ):
        await async_register_frontend(hass)
        await hass.async_block_till_done()

    handler, schema = hass.data[websocket_api.DOMAIN][CARD_FRESHNESS_COMMAND]
    handler(hass, connection, {"id": 7, "type": CARD_FRESHNESS_COMMAND})

    # A falsy schema is websocket_api's "no fields beyond the base message".
    assert not schema
    # Answered for a non-admin connection: the card runs as whoever is looking.
    connection.send_result.assert_called_once_with(7, {"cache_key": "3.3.0-abc123"})


@pytest.mark.parametrize(
    "make_resources",
    [
        pytest.param(
            lambda: MagicMock(spec=ResourceYAMLCollection),
            id="yaml_collection",
        ),
        pytest.param(MagicMock, id="other_collection_type"),
        pytest.param(lambda: None, id="lovelace_data_absent"),
    ],
)
async def test_register_frontend_falls_back_for_non_storage_resources(
    hass: HomeAssistant,
    make_resources: Callable[[], MagicMock | None],
) -> None:
    """Non-storage installations retain zero-configuration module loading.

    Covers `non-storage-writes-nothing` and `bundle-responses-cacheable`.
    """
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.update({"frontend", "lovelace"})
    resources = make_resources()
    if resources is not None:
        hass.data[LOVELACE_DATA] = LovelaceData("yaml", {}, resources, {})

    with (
        patch(
            "custom_components.adjustable_bed.frontend._gather",
            return_value=(True, "3.3.0", "3.3.0-abc123"),
        ),
        patch(
            "custom_components.adjustable_bed.frontend.add_extra_js_url"
        ) as add_extra_js_url,
    ):
        await async_register_frontend(hass)
        await async_register_frontend(hass)
        await hass.async_block_till_done()

    if resources is None:
        assert LOVELACE_DATA not in hass.data
    else:
        assert resources.method_calls == []
    add_extra_js_url.assert_called_once_with(
        hass,
        CARD_URL,
    )
    hass.http.async_register_static_paths.assert_awaited_once()
    assert _registered_static_paths(hass) == _expected_static_paths("3.3.0-abc123")
    assert hass.data[DOMAIN][DATA_FRONTEND_REGISTERED] is True


@pytest.mark.parametrize("missing", [CARD_FILENAME, CHUNK_FILENAME])
async def test_register_frontend_warns_when_half_the_bundle_is_missing(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing: str,
) -> None:
    """A half-built dist is diagnosable from the log, not from a network panel.

    Covers `missing-file-warning-covers-chunk`.
    """
    dist = _dist(tmp_path, monkeypatch)
    (dist / missing).unlink()
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    hass.config.components.update({"frontend", "lovelace"})
    resources = _storage_resources([])
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {}, resources, {})

    with caplog.at_level(logging.WARNING):
        await async_register_frontend(hass)
        await hass.async_block_till_done()

    assert str(dist) in caplog.text
    assert "bun run build" in caplog.text
    hass.http.async_register_static_paths.assert_not_awaited()
    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_not_awaited()
    resources.async_delete_item.assert_not_awaited()


async def test_register_frontend_waits_for_late_dependencies(
    hass: HomeAssistant,
) -> None:
    """A setup-order miss recovers when frontend and Lovelace become ready."""
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()

    with (
        patch(
            "custom_components.adjustable_bed.frontend._gather",
            return_value=(True, "3.3.0", "3.3.0-abc123"),
        ),
        patch(
            "custom_components.adjustable_bed.frontend._async_register_lovelace_resource",
            new_callable=AsyncMock,
            return_value=True,
        ) as register_resource,
        patch(
            "custom_components.adjustable_bed.frontend.add_extra_js_url"
        ) as add_extra_js_url,
    ):
        await async_register_frontend(hass)

        register_resource.assert_not_awaited()
        add_extra_js_url.assert_not_called()

        hass.config.components.add("frontend")
        hass.bus.async_fire_internal(
            EVENT_COMPONENT_LOADED,
            {"component": "frontend"},
        )
        await hass.async_block_till_done()
        add_extra_js_url.assert_called_once_with(
            hass,
            CARD_URL,
        )

        hass.config.components.add("lovelace")
        hass.bus.async_fire_internal(
            EVENT_COMPONENT_LOADED,
            {"component": "lovelace"},
        )
        await hass.async_block_till_done()
        register_resource.assert_awaited_once_with(
            hass,
            CARD_URL,
        )


@pytest.mark.parametrize("cache_key", ["3.7.1-old", "4.0.0-new"])
async def test_card_loader_serves_current_bundle_through_http(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    tmp_path: Path,
    cache_key: str,
) -> None:
    """Cold clients and saved old URLs load the bundle without authentication."""
    entry = b'import "./adjustable-bed-card-chunk.js";'
    chunk = b'customElements.define("test-bed-card", class extends HTMLElement {});'
    (tmp_path / CARD_FILENAME).write_bytes(entry)
    (tmp_path / CHUNK_FILENAME).write_bytes(chunk)
    assert await async_setup_component(hass, "http", {})

    with (
        patch("custom_components.adjustable_bed.frontend._dist_dir", return_value=tmp_path),
        patch(
            "custom_components.adjustable_bed.frontend._gather",
            return_value=(True, "test", cache_key),
        ),
    ):
        await async_register_frontend(hass)

    client = await hass_client_no_auth()
    current_url = _card_url(cache_key)
    for url in (CARD_URL, f"{CARD_URL}?v=3.2.1", _card_url("3.6.0-stale")):
        response = await client.get(url)
        assert response.status == 200
        assert response.content_type == "text/javascript"
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.text() == f"import {json.dumps(current_url)};\n"

    # The exact current route must win over the loader's version-path fallback.
    response = await client.get(current_url)
    assert response.status == 200
    assert response.content_type == "text/javascript"
    assert "max-age=" in response.headers["Cache-Control"]
    assert await response.read() == entry

    # The entry's relative import resolves to its sibling under the same segment.
    response = await client.get(_chunk_url(cache_key))
    assert response.status == 200
    assert "max-age=" in response.headers["Cache-Control"]
    assert await response.read() == chunk

    # The fallback serves only the loader, never arbitrary integration files.
    assert (await client.get(f"{URL_BASE}/manifest.json")).status == 404
    assert (await client.get(f"{URL_BASE}/old/manifest.json")).status == 404


async def test_card_loader_resolves_a_stale_chunk_path(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old chunk path answers with the pointer module instead of a 404.

    A service worker can hold an entry without the sibling chunk it imports.
    That entry then asks for the chunk under the cache key it was built with,
    which an upgrade has already moved past.
    """
    _dist(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "http", {})
    with patch(
        "custom_components.adjustable_bed.frontend._gather",
        return_value=(True, "test", "4.0.0-new"),
    ):
        await async_register_frontend(hass)
    current_url = _card_url("4.0.0-new")

    client = await hass_client_no_auth()
    response = await client.get(_chunk_url("3.7.1-old"))

    assert response.status == 200
    assert response.content_type == "text/javascript"
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.text() == f"import {json.dumps(current_url)};\n"


async def test_resource_migration_preserves_permanent_workaround(
    hass: HomeAssistant,
) -> None:
    """Restart keeps the working permanent URL and removes only old aliases."""
    resources = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    permanent = {CONF_ID: "manual", CONF_TYPE: "module", CONF_URL: CARD_URL}
    await resources.store.async_save(
        {
            "items": [
                {
                    CONF_ID: "previous-auto-resource",
                    CONF_TYPE: "module",
                    CONF_URL: _card_url("3.7.1-previous"),
                },
                permanent,
            ]
        }
    )
    hass.data[LOVELACE_DATA] = LovelaceData("storage", {}, resources, {})

    assert await _async_register_lovelace_resource(hass, CARD_URL)
    assert resources.async_items() == [permanent]
