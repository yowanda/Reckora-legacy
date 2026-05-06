"""Phase 6b — engine-aware probe heuristic tests."""

from __future__ import annotations

import httpx

from app.checker import Status
from app.engine_probes import ENGINE_PROBES
from app.sites import Site


def _site(*, engine: str, url: str = "https://example.com/{username}") -> Site:
    return Site(
        name="TestSite",
        url=url,
        url_main="https://example.com",
        check_type="message",
        engine=engine,
    )


def _response(status_code: int, body: str = "") -> httpx.Response:
    return httpx.Response(status_code=status_code, text=body)


# ---------------------------------------------------------------------------
# XenForo
# ---------------------------------------------------------------------------


class TestXenForo:
    engine = "XenForo"

    def test_404_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        result = probe(_response(404), "", _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-xenforo:404" in signals

    def test_200_with_user_id_returns_claimed(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = '<div class="memberProfilePage" data-user-id="12345">profile</div>'
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.CLAIMED
        assert "engine-xenforo:user-id-match" in signals

    def test_200_without_user_id_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = "<html><body>User not found</body></html>"
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-xenforo:no-user-id" in signals


# ---------------------------------------------------------------------------
# Discourse
# ---------------------------------------------------------------------------


class TestDiscourse:
    engine = "Discourse"

    def test_404_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        result = probe(_response(404), "", _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-discourse:404" in signals

    def test_200_with_json_username_returns_claimed(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = '{"user": {"username": "alice", "id": 42}}'
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.CLAIMED
        assert "engine-discourse:json-match" in signals

    def test_200_without_username_returns_unknown(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = '{"categories": []}'
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.UNKNOWN
        assert "engine-discourse:200-no-username" in signals


# ---------------------------------------------------------------------------
# vBulletin
# ---------------------------------------------------------------------------


class TestVBulletin:
    engine = "vBulletin"

    def test_404_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        result = probe(_response(404), "", _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-vbulletin:404" in signals

    def test_200_with_userinfo_returns_claimed(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = '<div id="userinfo"><h1>alice</h1></div>'
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.CLAIMED
        assert "engine-vbulletin:userinfo-match" in signals

    def test_200_without_userinfo_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = "<html><body>This user has not registered</body></html>"
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-vbulletin:no-userinfo" in signals


# ---------------------------------------------------------------------------
# uCoz
# ---------------------------------------------------------------------------


class TestUCoz:
    engine = "uCoz"

    def test_404_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        result = probe(_response(404), "", _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-ucoz:404" in signals

    def test_200_with_not_found_marker_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = "<html><body>User not found in this community</body></html>"
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-ucoz:not-found-marker" in signals

    def test_200_with_russian_not_found_returns_free(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = "<html><body>Пользователь не найден</body></html>"
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.FREE
        assert "engine-ucoz:not-found-marker" in signals

    def test_200_with_profile_returns_claimed(self) -> None:
        probe = ENGINE_PROBES[self.engine]
        body = "<html><body><div class='profile'>Alice's profile page</div></body></html>"
        result = probe(_response(200, body), body, _site(engine=self.engine))
        assert result is not None
        status, signals = result
        assert status is Status.CLAIMED
        assert "engine-ucoz:200-present" in signals
