"""Tests for Phase 7c — reverse-image lookup hook.

Two kinds of cases here:

* Pure-module unit tests over :func:`build_lookup_urls` and the
  ``is_lookupable_url`` guard. No FastAPI / DB needed.
* Endpoint tests via :class:`fastapi.testclient.TestClient` covering
  the auth gate, the ``enable_reverse_image`` knob, and the URL
  validation surface.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app.config import settings
from app.main import app
from app.reverseimage import PROVIDERS, build_lookup_urls, is_lookupable_url


def _safe_clear_overrides() -> None:
    """Best-effort wipe of ``admin_settings`` overrides — schema may not
    exist in pure-module test contexts."""
    with contextlib.suppress(Exception):
        from app.auth import db_connection

        with db_connection() as conn:
            conn.execute("DELETE FROM admin_settings")
    with contextlib.suppress(Exception):
        admin_settings_mod._invalidate_cache()


@pytest.fixture(autouse=True)
def _reset_state() -> Generator[None, None, None]:
    _safe_clear_overrides()
    yield
    _safe_clear_overrides()


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


class TestIsLookupableUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/avatar/123.png",
            "http://images.example.com/u/alice.jpg",
        ],
    )
    def test_accepts_http_https(self, url: str) -> None:
        assert is_lookupable_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "data:image/png;base64,AAAA",
            "javascript:alert(1)",
            "ftp://files.example.com/a.png",
            "//cdn.example.com/a.png",
            "",
            "alice@example.com",
        ],
    )
    def test_rejects_other_schemes_and_garbage(self, url: str) -> None:
        assert is_lookupable_url(url) is False


class TestBuildLookupUrls:
    def test_url_encodes_image_url(self) -> None:
        out = build_lookup_urls(
            "https://cdn.example.com/avatar/space%20me?x=1&y=2",
            templates={
                "yandex": "https://yandex.com/images/search?rpt=imageview&url={url}",
            },
        )
        # ``:`` and ``/`` are encoded so the upstream parser sees an
        # opaque value rather than a nested URL.
        assert out["yandex"] == (
            "https://yandex.com/images/search?rpt=imageview&url="
            "https%3A%2F%2Fcdn.example.com%2Favatar%2Fspace%2520me%3Fx%3D1%26y%3D2"
        )

    def test_returns_one_entry_per_supplied_template(self) -> None:
        out = build_lookup_urls(
            "https://x/y.png",
            templates={
                "yandex": "https://y/?url={url}",
                "google": "https://g/?url={url}",
                "tineye": "https://t/?url={url}",
            },
        )
        assert set(out) == {"yandex", "google", "tineye"}

    def test_skips_provider_without_template(self) -> None:
        # Provider not present in the templates dict is silently dropped.
        out = build_lookup_urls(
            "https://x/y.png",
            templates={"yandex": "https://y/?url={url}"},
        )
        assert set(out) == {"yandex"}
        assert "google" not in out
        assert "tineye" not in out

    def test_template_without_placeholder_returned_as_is(self) -> None:
        # Admins can supply opaque private mirrors that already inline
        # the URL via a different mechanism.
        out = build_lookup_urls(
            "https://x/y.png",
            templates={"yandex": "https://internal-mirror/static-search"},
        )
        assert out["yandex"] == "https://internal-mirror/static-search"

    def test_unsafe_url_returns_empty(self) -> None:
        out = build_lookup_urls(
            "data:image/png;base64,AAA",
            templates={"yandex": "https://y/?url={url}"},
        )
        assert out == {}

    def test_empty_template_dict_returns_empty(self) -> None:
        # No templates configured at all -> nothing to render.
        assert build_lookup_urls("https://x/y.png", templates={}) == {}


class TestProvidersMetadata:
    def test_provider_slugs_are_unique(self) -> None:
        slugs = [p.slug for p in PROVIDERS]
        assert len(slugs) == len(set(slugs))

    def test_each_provider_points_at_a_settings_attr(self) -> None:
        for p in PROVIDERS:
            assert hasattr(settings, p.settings_attr)


# ---------------------------------------------------------------------------
# Endpoint integration.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post(
        "/api/auth/login",
        json={"username": "rootadmin", "password": "rootpass1"},
    )
    assert r.status_code == 200, r.text
    return cast(str, r.json()["token"])


def _hdr(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


class TestEndpoint:
    def test_requires_auth(self, client: TestClient) -> None:
        r = client.post("/api/reverse-image", json={"image_url": "https://x/y.png"})
        assert r.status_code == 401

    def test_disabled_when_knob_off(self, client: TestClient, admin_token: str) -> None:
        admin_settings_mod.set_override("enable_reverse_image", 0, actor="rootadmin")
        r = client.post(
            "/api/reverse-image",
            headers=_hdr(admin_token),
            json={"image_url": "https://x/y.png"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["disabled"] is True
        assert body["providers"] == []

    def test_returns_three_providers_when_enabled(
        self, client: TestClient, admin_token: str
    ) -> None:
        admin_settings_mod.set_override("enable_reverse_image", 1, actor="rootadmin")
        r = client.post(
            "/api/reverse-image",
            headers=_hdr(admin_token),
            json={"image_url": "https://cdn.example.com/avatar/alice.png"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["disabled"] is False
        slugs = [p["slug"] for p in body["providers"]]
        assert slugs == ["yandex", "google", "tineye"]
        for p in body["providers"]:
            url = cast(str, p["url"])
            assert url.startswith(
                ("https://yandex.com", "https://lens.google.com", "https://tineye.com")
            )
            # The encoded image URL is embedded in every provider URL.
            assert "https%3A%2F%2Fcdn.example.com%2Favatar%2Falice.png" in url

    def test_rejects_data_uri(self, client: TestClient, admin_token: str) -> None:
        admin_settings_mod.set_override("enable_reverse_image", 1, actor="rootadmin")
        r = client.post(
            "/api/reverse-image",
            headers=_hdr(admin_token),
            json={"image_url": "data:image/png;base64,AAAA"},
        )
        assert r.status_code == 400

    def test_rejects_empty_image_url(self, client: TestClient, admin_token: str) -> None:
        admin_settings_mod.set_override("enable_reverse_image", 1, actor="rootadmin")
        # ``image_url`` has min_length=4 in the request model.
        r = client.post(
            "/api/reverse-image",
            headers=_hdr(admin_token),
            json={"image_url": "x"},
        )
        assert r.status_code == 422
