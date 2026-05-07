"""Phase 7a: perceptual-hash + edge stitching unit tests.

Covers ``app.imagephash``: ``compute_phash``, ``hamming_distance``,
``find_phash_edges``, and ``fetch_phash`` against a mocked httpx
transport.
"""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from app.imagephash import (
    HASH_HEX_LEN,
    compute_phash,
    fetch_phash,
    find_phash_edges,
    hamming_distance,
)

# ---------------------------------------------------------------------------
# Tiny image fixtures.
# ---------------------------------------------------------------------------


def _png(off: int = 0) -> bytes:
    """Return a 16x16 PNG whose pixel values are ``(x*16 + y*8 + off) & 0xff``.

    The deterministic gradient gives every (off) value a stable dHash
    so the tests don't depend on the encoder's exact bit pattern.
    """
    img = Image.new("L", (16, 16), 0)
    px = img.load()
    assert px is not None
    for y in range(16):
        for x in range(16):
            px[x, y] = (x * 16 + y * 8 + off) & 0xFF
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


# ---------------------------------------------------------------------------
# compute_phash
# ---------------------------------------------------------------------------


def test_compute_phash_returns_16_char_hex() -> None:
    h = compute_phash(_png())
    assert h is not None
    assert len(h) == HASH_HEX_LEN == 16
    int(h, 16)  # raises ValueError if not hex


def test_compute_phash_is_deterministic() -> None:
    """Hashing the same bytes twice must produce the same string."""
    bytes_ = _png(off=42)
    assert compute_phash(bytes_) == compute_phash(bytes_)


def test_compute_phash_returns_none_on_empty_bytes() -> None:
    assert compute_phash(b"") is None


def test_compute_phash_returns_none_on_invalid_image() -> None:
    """Garbage bytes must not raise — Pillow returns ``None``."""
    assert compute_phash(b"not an image, definitely") is None


def test_compute_phash_ignores_alpha_channel() -> None:
    """A pure-alpha-only difference must produce the same dHash because
    we collapse to grayscale before hashing."""
    rgb = Image.new("RGB", (16, 16))
    rgba = Image.new("RGBA", (16, 16))
    px_rgb = rgb.load()
    px_rgba = rgba.load()
    assert px_rgb is not None and px_rgba is not None
    for y in range(16):
        for x in range(16):
            v = (x * 16 + y * 8) & 0xFF
            px_rgb[x, y] = (v, v, v)
            # Same RGB but variable alpha.
            px_rgba[x, y] = (v, v, v, (x * 17) & 0xFF)
    bio_rgb = io.BytesIO()
    bio_rgba = io.BytesIO()
    rgb.save(bio_rgb, format="PNG")
    rgba.save(bio_rgba, format="PNG")
    assert compute_phash(bio_rgb.getvalue()) == compute_phash(bio_rgba.getvalue())


def test_compute_phash_close_for_small_offset_far_for_large() -> None:
    """A 1-pixel brightness shift should keep Hamming distance small;
    a large offset should push it well past the default threshold."""
    base = compute_phash(_png(off=0))
    near = compute_phash(_png(off=2))
    far = compute_phash(_png(off=128))
    assert base is not None and near is not None and far is not None
    assert hamming_distance(base, near) <= 6
    assert hamming_distance(base, far) >= 12


# ---------------------------------------------------------------------------
# hamming_distance
# ---------------------------------------------------------------------------


def test_hamming_distance_zero_for_identical() -> None:
    assert hamming_distance("0123456789abcdef", "0123456789abcdef") == 0


def test_hamming_distance_counts_bit_differences() -> None:
    # 0x0 vs 0xf differs in all 4 lowest bits.
    assert hamming_distance("0000000000000000", "000000000000000f") == 4
    # All bits flipped → maximum distance.
    assert hamming_distance("0000000000000000", "ffffffffffffffff") == 64


def test_hamming_distance_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError):
        hamming_distance("00", "0000")


# ---------------------------------------------------------------------------
# find_phash_edges
# ---------------------------------------------------------------------------


def test_find_phash_edges_returns_pairs_below_threshold() -> None:
    items = [
        ("github", "0000000000000000"),
        ("twitter", "0000000000000003"),  # 2 bits off
        ("reddit", "ffffffffffffffff"),
    ]
    edges = find_phash_edges(items, max_distance=4)
    keys = {(a, b) for a, b, _ in edges}
    assert ("github", "twitter") in keys
    # Distance 64 should not produce an edge.
    assert ("github", "reddit") not in keys
    assert ("reddit", "twitter") not in keys


def test_find_phash_edges_sorts_by_ascending_distance() -> None:
    items = [
        ("a", "0000000000000000"),
        ("b", "0000000000000001"),  # d=1
        ("c", "000000000000000f"),  # d=4 to a, d=3 to b
    ]
    edges = find_phash_edges(items, max_distance=8)
    distances = [d for _, _, d in edges]
    assert distances == sorted(distances)


def test_find_phash_edges_normalises_pair_ordering() -> None:
    """Even when input is reverse-sorted, emitted pairs are
    (lexicographic-min, lexicographic-max)."""
    items = [
        ("zeta", "0000000000000000"),
        ("alpha", "0000000000000003"),
    ]
    edges = find_phash_edges(items, max_distance=8)
    assert edges == [("alpha", "zeta", 2)]


def test_find_phash_edges_handles_empty_input() -> None:
    assert find_phash_edges([], max_distance=6) == []


def test_find_phash_edges_negative_max_distance_returns_empty() -> None:
    items = [("a", "0000000000000000"), ("b", "0000000000000000")]
    assert find_phash_edges(items, max_distance=-1) == []


def test_find_phash_edges_skips_mismatched_lengths() -> None:
    """Hashes of different widths can't be compared meaningfully — the
    function silently skips them rather than raising."""
    items = [
        ("a", "0000000000000000"),
        ("b", "00000000"),  # half-width
        ("c", "0000000000000001"),
    ]
    edges = find_phash_edges(items, max_distance=64)
    keys = {(a, b) for a, b, _ in edges}
    assert ("a", "c") in keys
    assert all("b" not in pair for pair in keys)


# ---------------------------------------------------------------------------
# fetch_phash
# ---------------------------------------------------------------------------


async def test_fetch_phash_returns_hash_on_200() -> None:
    body = _png(off=10)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        h = await fetch_phash(
            client,
            "https://example.com/avatar.png",
            max_bytes=1_000_000,
            timeout=2.0,
        )
    assert h == compute_phash(body)


async def test_fetch_phash_returns_none_on_404() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        h = await fetch_phash(
            client,
            "https://example.com/missing.png",
            max_bytes=1_000_000,
            timeout=2.0,
        )
    assert h is None


async def test_fetch_phash_returns_none_when_body_exceeds_cap() -> None:
    body = _png() * 50  # well past a 1 KB cap
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        h = await fetch_phash(
            client,
            "https://example.com/big.png",
            max_bytes=512,
            timeout=2.0,
        )
    assert h is None


async def test_fetch_phash_returns_none_on_connect_error() -> None:
    def _raise(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    transport = httpx.MockTransport(_raise)
    async with httpx.AsyncClient(transport=transport) as client:
        h = await fetch_phash(
            client,
            "https://example.com/avatar.png",
            max_bytes=1_000_000,
            timeout=2.0,
        )
    assert h is None


async def test_fetch_phash_returns_none_on_undecodable_body() -> None:
    """A 200 with non-image bytes must surface as ``None`` rather than
    bubbling Pillow's UnidentifiedImageError."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"plain text"))
    async with httpx.AsyncClient(transport=transport) as client:
        h = await fetch_phash(
            client,
            "https://example.com/text.png",
            max_bytes=1_000_000,
            timeout=2.0,
        )
    assert h is None
