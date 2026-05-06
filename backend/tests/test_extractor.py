"""Phase 3 extractor unit tests."""

from __future__ import annotations

from app.extractor import extract_identifiers, find_secondary_usernames


def test_og_meta_fallbacks_extracted() -> None:
    body = """
    <html><head>
      <meta property="og:title" content="Linus Torvalds (@torvalds)">
      <meta property="og:description" content="Created the Linux kernel.">
      <meta property="og:image" content="https://avatars.example.com/torvalds.png">
      <meta property="profile:username" content="torvalds">
    </head><body></body></html>
    """
    out = extract_identifiers(body)
    assert out["fullname"] == "Linus Torvalds (@torvalds)"
    assert out["bio"] == "Created the Linux kernel."
    assert out["avatar_url"].endswith("torvalds.png")
    assert out["username"] == "torvalds"


def test_email_and_social_links_extracted() -> None:
    body = """
    <html><body>
      <a href="mailto:linus@kernel.org">contact</a>
      <a href="https://github.com/torvalds">github</a>
      <a href="https://twitter.com/torvalds_t">twitter</a>
      <a href="https://t.me/torvalds_tg">telegram</a>
      <a href="https://reddit.com/user/torvalds_r">reddit</a>
    </body></html>
    """
    out = extract_identifiers(body)
    assert out["email"] == "linus@kernel.org"
    assert out["link_github"] == "https://github.com/torvalds"
    assert out["link_twitter"] == "https://twitter.com/torvalds_t"
    assert out["link_telegram"] == "https://t.me/torvalds_tg"
    assert out["link_reddit"] == "https://reddit.com/user/torvalds_r"


def test_image_emails_skipped() -> None:
    # Trackers and CDN sprite filenames sometimes look like emails.
    body = '<img src="cdn-tracker@2x.png">'
    out = extract_identifiers(body)
    assert "email" not in out


def test_extract_returns_empty_dict_on_unrecognised_html() -> None:
    out = extract_identifiers("<html><body><p>nothing here</p></body></html>")
    assert out == {}


def test_find_secondary_usernames_filters_root_and_generics() -> None:
    extracted = {
        "username": "torvalds",
        "link_twitter": "https://twitter.com/torvalds_t",
        "link_github": "https://github.com/torvalds",
        "link_facebook": "https://facebook.com/about",
        "link_reddit": "https://reddit.com/user/r_torv",
    }
    out = find_secondary_usernames(extracted, root_username="torvalds")
    found = {u for u, _ in out}
    # Root username should not feed back into the recursion.
    assert "torvalds" not in found
    # Generic platform pages must be skipped.
    assert "about" not in found
    # New, distinct usernames should make the cut.
    assert "torvalds_t" in found
    assert "r_torv" in found


def test_find_secondary_usernames_evidence_signal() -> None:
    extracted = {"link_keybase": "https://keybase.io/alice99"}
    out = find_secondary_usernames(extracted, root_username="bob")
    assert out == [("alice99", "link:keybase")]


def test_find_secondary_usernames_dedupes_across_signals() -> None:
    extracted = {
        "username": "alice99",
        "nickname": "alice99",
        "link_github": "https://github.com/alice99",
    }
    out = find_secondary_usernames(extracted, root_username="bob")
    # Same username across multiple signals should appear once.
    assert [u for u, _ in out].count("alice99") == 1


def test_socid_extractor_github_real_pattern_matches() -> None:
    # Trimmed-down version of the data-hydro-click attribute GitHub
    # ships on every profile page; socid_extractor's GitHub HTML scheme
    # only fires when ``github.githubassets.com`` is present in the body
    # (its anti-false-positive flag).
    body = (
        '<link rel="dns-prefetch" href="https://github.githubassets.com/">\n'
        '<button data-hydro-click="{&quot;event_type&quot;:&quot;analytics.event'
        "&quot;,&quot;payload&quot;:{&quot;profile_user_id&quot;:1024025,&quot;"
        "profile_user_login&quot;:&quot;torvalds&quot;,&quot;originating_url"
        '&quot;:&quot;https://github.com/torvalds&quot;}}">x</button>'
    )
    out = extract_identifiers(body)
    assert out.get("uid") == "1024025"
    assert out.get("username") == "torvalds"
