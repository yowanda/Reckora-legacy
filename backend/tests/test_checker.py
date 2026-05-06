"""Unit tests for the per-site checker."""
from __future__ import annotations

import httpx
import pytest

from app.checker import Status, check_site, is_valid_username
from app.sites import Site


def _site(**overrides) -> Site:
    base = {
        "name": "Example",
        "url": "https://example.com/{username}",
        "url_main": "https://example.com/",
        "check_type": "status_code",
    }
    base.update(overrides)
    return Site(**base)


@pytest.mark.parametrize(
    "username,expected",
    [
        ("alice", True),
        ("alice_42", True),
        ("a.b-c", True),
        ("", False),
        ("a" * 65, False),
        ("bad name", False),
        ("../etc/passwd", False),
    ],
)
def test_is_valid_username(username: str, expected: bool) -> None:
    assert is_valid_username(username) is expected


async def test_status_code_claimed() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="hi"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.CLAIMED
    assert result.http_status == 200


async def test_status_code_free_on_404() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.FREE


async def test_message_check_uses_presense_strs() -> None:
    site = _site(check_type="message", presense_strs=("user-profile",))
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>user-profile here</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice")
    assert result.status is Status.CLAIMED


async def test_message_check_uses_absence_strs() -> None:
    site = _site(check_type="message", absence_strs=("not found",))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="page says: not found"))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "alice")
    assert result.status is Status.FREE


async def test_skipped_when_regex_fails() -> None:
    site = _site(regex_check=r"^[a-z]+$")
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, site, "Alice123")
    assert result.status is Status.SKIPPED


async def test_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectTimeout("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_site(client, _site(), "alice")
    assert result.status is Status.ERROR
    assert result.error == "ConnectTimeout"
