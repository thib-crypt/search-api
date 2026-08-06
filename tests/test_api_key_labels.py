"""Labeled API keys (§6.2).

The invariant these protect: everything the server records about a caller is its
*label*. The key itself is compared and discarded — it never reaches a log line,
a metric, or a rate-limit bucket, truncated or otherwise.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import Depends
from starlette.requests import Request
from starlette.testclient import TestClient

from app.api.deps import get_api_key_label, matched_label, presented_key, require_api_key
from app.config import ANONYMOUS_LABEL, Settings, get_settings
from app.main import app

KEY = "sk_test_" + "a" * 32
OTHER = "sk_test_" + "b" * 32


def test_labeled_entries_are_parsed() -> None:
    settings = Settings(api_keys=f"app-mobile:{KEY},test:{OTHER}")
    assert settings.api_key_labels == {KEY: "app-mobile", OTHER: "test"}
    assert settings.api_key_set == {KEY, OTHER}


def test_bare_keys_stay_supported_and_get_a_derived_label() -> None:
    """Backward compatible: an existing API_KEYS=key1,key2 keeps working."""
    settings = Settings(api_keys=f"{KEY},{OTHER}")
    labels = settings.api_key_labels

    assert settings.api_key_set == {KEY, OTHER}
    assert len(set(labels.values())) == 2  # distinct keys get distinct labels


def test_derived_labels_are_stable_and_do_not_leak_the_key() -> None:
    first = Settings(api_keys=KEY).api_key_labels[KEY]
    second = Settings(api_keys=KEY).api_key_labels[KEY]

    assert first == second  # stable across restarts, so counters survive a reboot
    # Not a prefix/suffix of the key: a label ends up in logs and metrics, and a
    # partial key there is still key material.
    assert KEY[:8] not in first
    assert KEY[-8:] not in first
    assert KEY not in first


def test_labels_and_bare_keys_mix_in_one_setting() -> None:
    settings = Settings(api_keys=f"app:{KEY}, {OTHER} ")
    assert settings.api_key_labels[KEY] == "app"
    assert settings.api_key_labels[OTHER].startswith("key-")


def test_blank_and_malformed_entries_are_dropped() -> None:
    assert Settings(api_keys=" , ,").api_key_labels == {}
    # A trailing colon has no key, so the whole entry is treated as the key.
    assert list(Settings(api_keys="label:").api_key_labels) == ["label:"]


def test_matched_label_finds_the_right_key() -> None:
    labels = {KEY: "app-mobile", OTHER: "test"}
    assert matched_label(KEY, labels) == "app-mobile"
    assert matched_label(OTHER, labels) == "test"
    assert matched_label("nope", labels) is None
    assert matched_label(KEY, {}) is None


def test_presented_key_reads_both_headers() -> None:
    assert presented_key(None, KEY) == KEY
    assert presented_key(f"Bearer {KEY}", None) == KEY
    assert presented_key(f"bearer {KEY}", None) == KEY
    assert presented_key("Basic zzz", None) is None
    assert presented_key(None, None) is None


# -- Through the app --------------------------------------------------------


@pytest.fixture
def labeled(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", f"app-mobile:{KEY},test:{OTHER}")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_whole_stack_hands_the_handler_a_label(labeled) -> None:
    """End to end: a route behind `require_api_key` can read the caller's label."""

    async def probe(request: Request) -> dict:
        return {"label": get_api_key_label(request)}

    app.add_api_route("/__label_probe", probe, dependencies=[Depends(require_api_key)])
    try:
        with TestClient(app) as client:
            assert client.get("/__label_probe", headers={"X-API-Key": KEY}).json() == {
                "label": "app-mobile"
            }
            assert client.get(
                "/__label_probe", headers={"Authorization": f"Bearer {OTHER}"}
            ).json() == {"label": "test"}
            assert client.get("/__label_probe").status_code == 401
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__label_probe"
        ]


async def test_require_api_key_sets_the_matched_label(labeled) -> None:
    request = Request({"type": "http", "headers": [], "state": {}})
    await require_api_key(request, authorization=None, x_api_key=OTHER, settings=get_settings())
    assert request.state.api_key_label == "test"


async def test_auth_disabled_labels_every_caller_anonymous(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        request = Request({"type": "http", "headers": [], "state": {}})
        await require_api_key(request, authorization=None, x_api_key=None, settings=get_settings())
        assert request.state.api_key_label == ANONYMOUS_LABEL
    finally:
        get_settings.cache_clear()


def test_a_rejected_request_never_logs_the_key(labeled, caplog) -> None:
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        client.get("/v1/jobs/nope", headers={"X-API-Key": KEY})
        client.get("/v1/jobs/nope", headers={"X-API-Key": "sk_wrong_key_value"})

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert KEY not in logged
    assert "sk_wrong_key_value" not in logged
    assert KEY[:12] not in logged
