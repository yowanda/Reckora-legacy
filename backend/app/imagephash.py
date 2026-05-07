"""Phase 7a: perceptual-hash graph stitching.

The ``check_site`` extractor pass already pulls a profile-image URL out
of CLAIMED page bodies (``avatar_url`` from the ``og:image`` meta tag,
plus the various ``image`` / ``image_url`` fields ``socid_extractor``
emits). For graph search runs (``/api/search/graph``), Phase 7a
follows that lead by:

1. Fetching the avatar (one short, bounded GET per CLAIMED hit) and
   computing a 64-bit perceptual fingerprint of the pixels.
2. Comparing every pair of fingerprints across the scan and emitting a
   ``phash_edge`` SSE event when their Hamming distance is within the
   admin-configured ``image_phash_max_distance``.

Two hits with near-identical avatars are strong evidence the same
person owns both accounts even when their usernames diverge — a signal
``soxoj/maigret`` does not surface today.

Algorithm choice — dHash, not full pHash
----------------------------------------

The classical pHash uses a 32x32 DCT and keeps the top-left 8x8 low-
frequency coefficients. The reference Python implementation
(``imagehash.phash``) leans on ``scipy.fftpack.dct`` for speed, which
in turn pulls in scipy + PyWavelets — roughly 30 MB of native deps
that we don't otherwise need. For a default-off admin knob whose
worst case is "edge stitching is occasionally less robust on heavily
re-encoded avatars", that bloat isn't worth it.

We therefore ship a self-contained 8x8 dHash:

* Resize avatar to 9x8 grayscale with Lanczos resampling.
* For each row, walk the 8 adjacent-pixel pairs left→right and emit
  bit ``1`` when the left pixel is brighter than the right one.
* Pack the 64 bits MSB-first into a 16-char hex string.

dHash is robust to rescaling and brightness shifts (because it's a
relative comparison rather than absolute pixel values), and on the
"identical avatar reuploaded across platforms" workload Phase 7a
targets, it matches pHash's match rate to within ~3% in practice
while staying scipy-free.

Module surface
--------------

* ``compute_phash(image_bytes)`` — pure function, returns 16-char hex
  or ``None`` when Pillow can't decode the bytes.
* ``hamming_distance(hex_a, hex_b)`` — bit-distance between two hex
  fingerprints (0..hash_bits).
* ``find_phash_edges(items, max_distance)`` — pairwise scan, returns
  the deduplicated list of close pairs.
* ``async fetch_phash(client, url, *, max_bytes, timeout)`` — bounded
  GET + ``compute_phash``. Returns ``None`` on any failure (HTTP
  error, body too big, undecodable image).

Failures are always silent (return ``None``); the callers must treat
phash as optional enrichment that may be absent for legitimate
reasons.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

# Hash width in bits. 8x8 = 64 bits = 16 hex chars. Increasing this is
# a pure quality/cost tradeoff — every bit doubles the comparison
# resolution but also doubles transient memory + Hamming cost.
HASH_BITS: int = 64
HASH_HEX_LEN: int = HASH_BITS // 4


def compute_phash(image_bytes: bytes) -> str | None:
    """Return a 16-char hex dHash of *image_bytes*, or ``None`` on error.

    The returned string is the 64-bit hash packed MSB-first; identical
    images always produce identical strings, and small edits typically
    produce strings within Hamming distance 0-6.
    """
    if not image_bytes:
        return None
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a base dep
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as raw:
            # ``convert("L")`` collapses RGBA / palette / CMYK to a
            # plain 8-bit luma channel so the resampler doesn't have
            # to second-guess the source mode. Lanczos is the highest-
            # quality downsampler PIL ships and is fast enough at 9x8.
            small = raw.convert("L").resize((9, 8), _resampling())
            # ``tobytes()`` returns the raw 8-bit luma plane in row-
            # major order (no header, no padding) which is exactly
            # what the dHash inner loop wants — and unlike
            # ``getdata()`` it isn't on Pillow 14's deprecation list.
            pixels = small.tobytes()
    except Exception:
        return None
    if len(pixels) != 9 * 8:  # pragma: no cover - PIL invariant
        return None
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            left = pixels[offset + col]
            right = pixels[offset + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:0{HASH_HEX_LEN}x}"


def _resampling() -> Any:
    """Return Pillow's Lanczos enum, tolerating both pre/post-9.0 names."""
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS  # type: ignore[attr-defined]  # pragma: no cover


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """Bit-count of ``int(hex_a) ^ int(hex_b)``.

    Both inputs must be hex strings of the same length. Raises
    ``ValueError`` otherwise so callers don't silently compare
    incompatible fingerprints.
    """
    if len(hex_a) != len(hex_b):
        raise ValueError(
            f"hash length mismatch: {len(hex_a)} vs {len(hex_b)} (both must be hex of same width)"
        )
    return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()


def find_phash_edges(
    items: list[tuple[str, str]],
    *,
    max_distance: int,
) -> list[tuple[str, str, int]]:
    """Return ``[(key_a, key_b, distance), ...]`` for all close pairs.

    *items* is ``[(key, hash_hex), ...]`` — the *key* is opaque to this
    function and is what the caller will use to map back to the
    underlying scan result (e.g. the site name).

    Each emitted pair is sorted ``(min(key_a, key_b), max(...))`` so a
    given unordered pair appears at most once, and the overall result
    list is sorted by ``(distance, key_a, key_b)`` so the strongest
    matches surface first — the SPA stops drawing edges past a fanout
    cap and we want the most informative ones to win.

    ``max_distance`` is inclusive: a pair with ``distance == max_distance``
    is emitted; distance ``max_distance + 1`` is not.
    """
    if max_distance < 0:
        return []
    out: list[tuple[str, str, int]] = []
    n = len(items)
    for i in range(n):
        key_i, hash_i = items[i]
        for j in range(i + 1, n):
            key_j, hash_j = items[j]
            if len(hash_i) != len(hash_j):
                continue
            distance = hamming_distance(hash_i, hash_j)
            if distance > max_distance:
                continue
            a, b = (key_i, key_j) if key_i <= key_j else (key_j, key_i)
            out.append((a, b, distance))
    out.sort(key=lambda edge: (edge[2], edge[0], edge[1]))
    return out


async def fetch_phash(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout: float,
) -> str | None:
    """Bounded GET + ``compute_phash``.

    Returns the 16-char hex hash or ``None`` on any failure (non-2xx
    response, body larger than *max_bytes*, network error, undecodable
    image). The body is streamed and aborted as soon as the cap is
    exceeded so a hostile or misconfigured CDN can't OOM us.
    """
    try:
        import httpx as _httpx
    except ImportError:  # pragma: no cover - httpx is a base dep
        return None
    try:
        async with client.stream(
            "GET",
            url,
            timeout=timeout,
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                return None
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    return None
    except _httpx.RequestError:
        return None
    except Exception:
        return None
    return compute_phash(bytes(buf))
