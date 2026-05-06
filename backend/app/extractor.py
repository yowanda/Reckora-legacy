"""Phase 3: profile-identity extraction from CLAIMED page bodies.

Wraps ``socid_extractor`` (MIT, the same lib maigret uses) to pull
structured identity fields (real name, uid, location, bio, alt usernames,
social links) from a CLAIMED result's HTML body.  On top of the
socid_extractor schemes we run a handful of lightweight generic regexes
for mailto: links, common social-link ``<a>`` patterns, and Open-Graph /
meta tags.

The module is intentionally pure-string/regex with **no I/O** — it
operates on a body the caller already fetched.
"""
from __future__ import annotations

import re


def extract_identifiers(body: str, url: str = "") -> dict[str, str]:
    """Return a flat ``{field: value}`` dict of identity signals found in *body*.

    Merges results from socid_extractor (regex-flag-based schemes) with our
    own generic fallbacks (OG tags, mailto, social ``<a>`` patterns).
    Keys are lowercase, human-readable strings like ``"uid"``, ``"username"``,
    ``"fullname"``, ``"email"``, ``"bio"``, ``"location"``, ``"link_github"``.
    """
    merged: dict[str, str] = {}

    # 1) socid_extractor — the heavy lifter for site-specific regexes.
    try:
        from socid_extractor import extract as _se_extract

        raw = _se_extract(body)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if v is not None and str(v).strip():
                    merged[str(k).lower().strip()] = str(v).strip()
    except Exception:
        pass

    # 2) Generic OG / meta fallbacks — lots of sites expose these even
    # when socid_extractor has no dedicated scheme.
    merged.update(_extract_og_meta(body))

    # 3) Mailto links and social hrefs in the body.
    emails = _extract_emails(body)
    if emails:
        merged.setdefault("email", emails[0])

    for label, href in _extract_social_links(body):
        key = f"link_{label}"
        merged.setdefault(key, href)

    return merged


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_OG_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']og:(\w+)["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_PROFILE_META_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']profile:(\w+)["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

_OG_KEY_MAP: dict[str, str] = {
    "title": "fullname",
    "description": "bio",
    "image": "avatar_url",
    "url": "profile_url",
}
_PROFILE_KEY_MAP: dict[str, str] = {
    "username": "username",
    "first_name": "first_name",
    "last_name": "last_name",
    "gender": "gender",
}


def _extract_og_meta(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _OG_RE.finditer(body):
        key = _OG_KEY_MAP.get(m.group(1).lower())
        if key:
            val = m.group(2).strip()
            if val:
                out.setdefault(key, val)
    for m in _PROFILE_META_RE.finditer(body):
        key = _PROFILE_KEY_MAP.get(m.group(1).lower())
        if key:
            val = m.group(2).strip()
            if val:
                out.setdefault(key, val)
    return out


_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}")


def _extract_emails(body: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _EMAIL_RE.finditer(body):
        email = m.group(0).lower()
        if email not in seen and not email.endswith((".png", ".jpg", ".gif", ".svg")):
            seen.add(email)
            out.append(email)
            if len(out) >= 5:
                break
    return out


# Each pattern matches a complete social profile URL whose final captured
# group is the platform-side username. We use the URL pattern itself for
# both link discovery (paired with an href= anchor below) and for
# extracting the username out of an already-known URL.
_SOCIAL_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("github", re.compile(r'https?://github\.com/([A-Za-z0-9_.\-]+)/?', re.I)),
    ("twitter", re.compile(r'https?://(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/?', re.I)),
    ("instagram", re.compile(r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?', re.I)),
    ("linkedin", re.compile(r'https?://(?:www\.)?linkedin\.com/in/([A-Za-z0-9_.\-]+)/?', re.I)),
    ("facebook", re.compile(r'https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.]+)/?', re.I)),
    ("youtube", re.compile(r'https?://(?:www\.)?youtube\.com/(?:@|channel/|user/)([A-Za-z0-9_.\-]+)/?', re.I)),
    ("telegram", re.compile(r'https?://t\.me/([A-Za-z0-9_]+)/?', re.I)),
    ("reddit", re.compile(r'https?://(?:www\.)?reddit\.com/user/([A-Za-z0-9_.\-]+)/?', re.I)),
    ("mastodon", re.compile(r'https?://[a-z0-9.\-]+/@([A-Za-z0-9_]+)/?', re.I)),
    ("keybase", re.compile(r'https?://keybase\.io/([A-Za-z0-9_]+)/?', re.I)),
]

_HREF_RE = re.compile(r'''href=["']([^"']+)["']''', re.IGNORECASE)

# Username-like values we always skip (too generic to be real alt-accounts,
# or platform/host names that show up in cross-references rather than as
# real account identifiers).
_SKIP_USERNAMES: frozenset[str] = frozenset({
    # Generic site paths
    "about", "search", "login", "signup", "register", "help", "terms",
    "privacy", "settings", "admin", "home", "index", "static", "assets",
    "api", "undefined", "null", "none", "share", "explore", "notifications",
    "messages", "tags", "categories", "trending", "discover", "browse",
    "popular", "new", "top", "feed", "stories", "watch", "channel", "user",
    "channels", "users", "profile", "profiles", "account", "accounts",
    "dashboard", "embed", "widget", "auth", "oauth", "callback",
    # Platform names that frequently appear in <a> chrome / footers
    "github", "twitter", "facebook", "instagram", "linkedin", "youtube",
    "reddit", "telegram", "tiktok", "pinterest", "tumblr", "twitch",
    "mastodon", "keybase", "spotify", "soundcloud", "vimeo", "flickr",
    "smugmug", "wordpress", "blogger", "medium", "substack", "patreon",
    "discord", "slack", "skype", "snapchat", "whatsapp", "wechat",
    "verge", "theverge", "envato", "launchpadstatus",
})


_TLD_SUFFIXES: tuple[str, ...] = ("com", "net", "org", "io", "co", "app", "dev")


def _looks_like_username(value: str) -> bool:
    """Heuristic check that *value* could plausibly be a username.

    Filters out the long tail of URL slugs, file basenames, hash-like
    channel IDs and product names that the broad social-link regex
    captures by accident.
    """
    if len(value) < 3 or len(value) > 20:
        return False
    if "." in value:  # filenames (foo.png), versions (v1.2)
        return False
    if value.isdigit():  # bare numeric id rarely matches a real username
        return False
    # YouTube channel IDs are 22-24 chars and start with 'uc' (case-insensitive).
    # Even when truncated below 22, they are unrecognisable as usernames.
    if len(value) >= 16 and value.startswith("uc"):
        return False
    if value in _SKIP_USERNAMES:
        return False
    # Strip trailing TLD-like suffixes ("wordpresscom", "envatoio")
    # then re-check against the skip list.
    for tld in _TLD_SUFFIXES:
        if value.endswith(tld) and len(value) > len(tld) + 2:
            stem = value[: -len(tld)]
            if stem in _SKIP_USERNAMES or stem.removesuffix("dot") in _SKIP_USERNAMES:
                return False
    return True


def _extract_social_links(body: str) -> list[tuple[str, str]]:
    """Yield ``[(platform_label, full_url), ...]`` for every distinct social
    profile URL referenced from an ``href=`` in *body*.
    """
    out: list[tuple[str, str]] = []
    matched_labels: set[str] = set()
    for href_match in _HREF_RE.finditer(body):
        href = href_match.group(1)
        for label, pat in _SOCIAL_URL_PATTERNS:
            if label in matched_labels:
                continue
            if pat.fullmatch(href.rstrip("/")) or pat.fullmatch(href):
                matched_labels.add(label)
                out.append((label, href))
                break
    return out


# ---------------------------------------------------------------------------
# Secondary-username discovery
# ---------------------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def find_secondary_usernames(
    extracted: dict[str, str],
    root_username: str,
) -> list[tuple[str, str]]:
    """Return ``[(username, evidence_signal), ...]`` for identities found in
    *extracted* that look like distinct usernames worth a follow-up scan.

    ``root_username`` is excluded to avoid self-loops.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = {root_username.lower()}

    def _maybe_add(value: str, signal: str) -> None:
        clean = value.strip().lstrip("@").lower()
        if (
            clean
            and clean not in seen
            and _USERNAME_RE.match(clean)
            and _looks_like_username(clean)
        ):
            seen.add(clean)
            candidates.append((clean, signal))

    # Direct fields that look like usernames.
    for key in ("username", "uid", "nickname", "login", "screen_name"):
        val = extracted.get(key)
        if val:
            _maybe_add(val, f"extracted:{key}")

    # Social links — the captured group is the username on that platform.
    for key, val in extracted.items():
        if key.startswith("link_"):
            platform = key[5:]
            uname = _username_from_social_url(val)
            if uname:
                _maybe_add(uname, f"link:{platform}")

    return candidates


def _username_from_social_url(url: str) -> str | None:
    """Pull the username segment out of a social-profile URL."""
    for _, pat in _SOCIAL_URL_PATTERNS:
        m = pat.fullmatch(url.rstrip("/"))
        if m:
            return m.group(1)
    return None
