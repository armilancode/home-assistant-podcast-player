"""Tests for bundled Podcast Player frontend registration."""

from types import SimpleNamespace

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE, MODE_YAML

from custom_components.podcast_player.frontend import (
    CARD_MODULE_URL,
    CARD_PATH,
    CARD_URL_PATH,
    FRONTEND_STATUS_KEY,
    STATIC_REGISTERED_KEY,
    _is_legacy_card_url,
    async_setup_card_frontend,
    async_unload_card_frontend,
)


class FakeHttp:
    """Capture registered static paths."""

    def __init__(self) -> None:
        self.static_paths = []

    async def async_register_static_paths(self, paths) -> None:
        """Record static path registrations."""
        self.static_paths.extend(paths)


class FakeResources:
    """Minimal Lovelace resource collection."""

    def __init__(self, items: list[dict] | None = None, fail_ids: set[str] | None = None) -> None:
        self.items = items or []
        self.deleted: list[str] = []
        self.fail_ids = fail_ids or set()

    async def async_get_info(self) -> dict[str, int]:
        """Mark the collection as loaded."""
        return {"resources": len(self.items)}

    def async_items(self) -> list[dict]:
        """Return configured resources."""
        return list(self.items)

    async def async_delete_item(self, item_id: str) -> None:
        """Record and remove a resource."""
        self.deleted.append(item_id)
        if item_id in self.fail_ids:
            raise RuntimeError("storage write failed")
        self.items = [item for item in self.items if item.get("id") != item_id]


def _hass(*, resources: FakeResources | None = None, resource_mode: str = MODE_STORAGE):
    changes: list[tuple[str, str]] = []
    module_manager = UrlManager(lambda change, url: changes.append((change, url)), [])
    data = {DATA_EXTRA_MODULE_URL: module_manager}
    if resources is not None:
        data[LOVELACE_DATA] = SimpleNamespace(
            resources=resources,
            resource_mode=resource_mode,
        )
    hass = SimpleNamespace(
        config=SimpleNamespace(components={"frontend", "lovelace"}),
        data=data,
        http=FakeHttp(),
    )
    return hass, module_manager, changes


def test_legacy_card_url_matching_is_exact_and_query_safe() -> None:
    """Migration identifies documented old card resources without broad matches."""
    assert _is_legacy_card_url("/local/podcast-player-card/podcast-player-card.js?v=old")
    assert _is_legacy_card_url("https://ha.example/local/podcast-player-card/podcast-player-card.js?v=old")
    assert not _is_legacy_card_url("/local/another-player/podcast-player-card.js")
    assert not _is_legacy_card_url(None)


async def test_fresh_install_serves_and_loads_bundled_card() -> None:
    """A fresh integration setup needs no Lovelace resource or www file."""
    hass, manager, changes = _hass()

    status = await async_setup_card_frontend(hass)

    assert status == {
        "available": True,
        "version": "0.3.0-alpha.3",
        "module_url": CARD_MODULE_URL,
        "legacy_resources_removed": 0,
        "legacy_yaml_resource_present": False,
    }
    assert manager.urls == frozenset({CARD_MODULE_URL})
    assert changes == [("added", CARD_MODULE_URL)]
    assert hass.data[STATIC_REGISTERED_KEY] is True
    assert len(hass.http.static_paths) == 1
    path = hass.http.static_paths[0]
    assert path.url_path == CARD_URL_PATH
    assert path.path == str(CARD_PATH)
    assert path.cache_headers is True
    assert CARD_PATH.is_file()

    second_status = await async_setup_card_frontend(hass)
    assert second_status["available"] is True
    assert len(hass.http.static_paths) == 1
    assert changes == [("added", CARD_MODULE_URL)]


async def test_storage_collection_without_legacy_resource_is_unchanged() -> None:
    """Existing unrelated Lovelace resources are not touched."""
    resources = FakeResources(
        [{"id": "other", "url": "/local/other-card.js", "type": "module"}]
    )
    hass, _, _ = _hass(resources=resources)

    status = await async_setup_card_frontend(hass)

    assert status["legacy_resources_removed"] == 0
    assert resources.deleted == []


async def test_upgrade_removes_only_legacy_storage_resource() -> None:
    """Storage-mode upgrades remove the obsolete manual resource after registration."""
    resources = FakeResources(
        [
            {
                "id": "legacy",
                "url": "/local/podcast-player-card/podcast-player-card.js?v=0.3.0-alpha.2",
                "type": "module",
            },
            {"id": "other", "url": "/local/other-card.js", "type": "module"},
        ]
    )
    hass, manager, _ = _hass(resources=resources)
    manager.add("/podcast_player/podcast-player-card.js?v=0.3.0-alpha.2")

    status = await async_setup_card_frontend(hass)

    assert status["legacy_resources_removed"] == 1
    assert resources.deleted == ["legacy"]
    assert [item["id"] for item in resources.items] == ["other"]
    assert manager.urls == frozenset({CARD_MODULE_URL})


async def test_yaml_resource_is_reported_but_not_mutated() -> None:
    """YAML-owned Lovelace configuration remains entirely user-managed."""
    resources = FakeResources(
        [{"url": "/local/podcast-player-card/podcast-player-card.js", "type": "module"}]
    )
    hass, _, _ = _hass(resources=resources, resource_mode=MODE_YAML)

    status = await async_setup_card_frontend(hass)

    assert status["legacy_resources_removed"] == 0
    assert status["legacy_yaml_resource_present"] is True
    assert resources.deleted == []


async def test_bad_legacy_entries_do_not_block_card_registration() -> None:
    """Missing ids and failed Lovelace writes are contained during migration."""
    legacy_url = "/local/podcast-player-card/podcast-player-card.js?v=old"
    resources = FakeResources(
        [
            {"url": legacy_url, "type": "module"},
            {"id": "broken", "url": legacy_url, "type": "module"},
        ],
        fail_ids={"broken"},
    )
    hass, manager, _ = _hass(resources=resources)

    status = await async_setup_card_frontend(hass)

    assert status["available"] is True
    assert status["legacy_resources_removed"] == 0
    assert resources.deleted == ["broken"]
    assert CARD_MODULE_URL in manager.urls


async def test_missing_frontend_keeps_backend_available() -> None:
    """Headless Home Assistant can still use Podcast Player's backend."""
    hass = SimpleNamespace(
        config=SimpleNamespace(components=set()),
        data={},
        http=FakeHttp(),
    )

    status = await async_setup_card_frontend(hass)

    assert status["available"] is False
    assert hass.data[FRONTEND_STATUS_KEY] == status
    assert hass.http.static_paths == []


async def test_unload_removes_advertised_module_but_keeps_static_route() -> None:
    """Entry unload stops advertising the module without re-registering routes."""
    hass, manager, changes = _hass()
    await async_setup_card_frontend(hass)

    async_unload_card_frontend(hass)

    assert manager.urls == frozenset()
    assert changes[-1] == ("removed", CARD_MODULE_URL)
    assert hass.data[FRONTEND_STATUS_KEY]["available"] is False
    assert hass.data[STATIC_REGISTERED_KEY] is True


def test_unload_without_frontend_state_is_a_safe_noop() -> None:
    """Partial setup can unload without a module manager or status record."""
    hass = SimpleNamespace(data={})

    async_unload_card_frontend(hass)

    assert hass.data == {}
