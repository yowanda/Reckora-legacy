"""Phase 10b — catalog parity / tentative-sites opt-in.

Sites that previously hit the build-time precision quality gate (e.g.
returned ambiguous 200s on any path so a username probe couldn't
classify cleanly) used to be *trimmed* from ``sites.json`` entirely.
Phase 10b ships them with ``tentative=true`` so an operator can opt-in
for max-recall scans via the ``enable_tentative_sites`` admin knob,
while the default-off behaviour keeps the pre-Phase-10b probe set
byte-for-byte.

These tests exercise the four contract points end-to-end:

1. The ``Site`` dataclass loads ``tentative`` + ``tentative_reason``
   from ``sites.json`` and auto-injects the ``"tentative"`` tag for
   filter compatibility.
2. ``site_to_raw`` serialises tentative + reason back into the JSON
   shape the admin DB editor consumes (round-trip).
3. ``SiteCatalog.filtered`` honours the ``enable_tentative_sites``
   knob (default 0 hides them, 1 includes them, explicit
   ``include_tentative=True/False`` overrides the knob).
4. ``checker._confidence`` caps a tentative-site CLAIMED row at
   ``max_tentative_confidence`` so the Phase 5h precision floor still
   demotes it to UNKNOWN unless ML + verify both vouch strongly.
5. The two new knobs round-trip via PATCH ``/api/admin/settings`` and
   are surfaced by ``describe()`` / ``effective_map()``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import admin_settings as admin_settings_mod
from app import sites as sites_mod
from app.checker import Status, _confidence
from app.main import app
from app.sites import Site, SiteCatalog, _coerce_site, site_to_raw


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """Phase 5d-style module-scoped client. We don't actually call the
    HTTP API in this module — but instantiating the TestClient runs
    the FastAPI lifespan, which bootstraps the SQLite admin DB so
    ``admin_settings.set_override`` has a real ``admin_settings`` table
    to upsert into."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Site dataclass + coerce/raw round-trip
# ---------------------------------------------------------------------------


class TestSiteDataclassTentative:
    def test_default_is_not_tentative(self) -> None:
        s = Site(
            name="x", url="https://x/{username}", url_main="https://x/", check_type="status_code"
        )
        assert s.tentative is False
        assert s.tentative_reason is None
        # Auto-injected tag stays empty for non-tentative sites.
        assert "tentative" not in s.tags

    def test_coerce_propagates_tentative_and_auto_tags(self) -> None:
        raw = {
            "url": "https://x/{username}",
            "urlMain": "https://x/",
            "checkType": "status_code",
            "tags": ["forum"],
            "tentative": True,
            "tentativeReason": "returns_200_on_any_path",
        }
        s = _coerce_site("ExampleForum", raw)
        assert s is not None
        assert s.tentative is True
        assert s.tentative_reason == "returns_200_on_any_path"
        # ``tentative`` tag is auto-injected so the existing tag
        # filter ("filter by tag X") matches without a special case.
        assert "tentative" in s.tags
        assert "forum" in s.tags

    def test_coerce_does_not_double_tag_when_json_already_lists_tentative(self) -> None:
        raw = {
            "url": "https://x/{username}",
            "urlMain": "https://x/",
            "checkType": "status_code",
            "tags": ["forum", "tentative"],
            "tentative": True,
        }
        s = _coerce_site("ExampleForum", raw)
        assert s is not None
        # Exactly one ``tentative`` entry — never duplicated.
        assert s.tags.count("tentative") == 1

    def test_site_to_raw_round_trips_tentative(self) -> None:
        # Round-trip a tentative site through coerce → raw → coerce.
        original = {
            "url": "https://x/{username}",
            "urlMain": "https://x/",
            "checkType": "status_code",
            "tags": ["forum"],
            "tentative": True,
            "tentativeReason": "requires_login_for_classify",
        }
        site = _coerce_site("ExampleForum", original)
        assert site is not None
        out = site_to_raw(site)
        assert out["tentative"] is True
        assert out["tentativeReason"] == "requires_login_for_classify"
        # The auto-injected ``tentative`` tag is *not* persisted —
        # ``_coerce_site`` re-derives it from the boolean on next load.
        assert "tentative" not in (out.get("tags") or [])
        # Re-coerce the serialised form and confirm we land on the
        # same in-memory site.
        site2 = _coerce_site("ExampleForum", out)
        assert site2 is not None
        assert site2.tentative == site.tentative
        assert site2.tentative_reason == site.tentative_reason
        assert "tentative" in site2.tags

    def test_site_to_raw_omits_fields_for_non_tentative(self) -> None:
        s = Site(
            name="x",
            url="https://x/{username}",
            url_main="https://x/",
            check_type="status_code",
            tags=("forum",),
        )
        out = site_to_raw(s)
        assert "tentative" not in out
        assert "tentativeReason" not in out


# ---------------------------------------------------------------------------
# SiteCatalog.filtered — knob-driven probe-set gating
# ---------------------------------------------------------------------------


def _catalog(*sites: Site) -> SiteCatalog:
    return SiteCatalog(tuple(sites))


def _site(name: str, *, tentative: bool = False, tags: tuple[str, ...] = ()) -> Site:
    return Site(
        name=name,
        url=f"https://{name.lower()}/{{username}}",
        url_main=f"https://{name.lower()}/",
        check_type="status_code",
        tags=tags + (("tentative",) if tentative and "tentative" not in tags else ()),
        tentative=tentative,
    )


@pytest.fixture(autouse=True)
def _reset_admin_state(client: TestClient) -> None:
    """Make sure neither the admin-settings cache nor persisted DB
    overrides bleed knob values across tests in this module — every
    test gets a clean slate. The test order otherwise depends on
    pytest's collection order, which we don't want to couple to.

    Depending on the module-scoped ``client`` fixture forces the app
    lifespan (and therefore the SQLite admin DB bootstrap) to run
    before we touch ``admin_settings`` so ``delete_override`` doesn't
    blow up on a missing table."""
    admin_settings_mod.delete_override("enable_tentative_sites")
    admin_settings_mod.delete_override("max_tentative_confidence")
    admin_settings_mod._invalidate_cache()


class TestFilteredHonoursTentativeKnob:
    def test_knob_off_hides_tentative_sites(self, client: TestClient) -> None:
        cat = _catalog(_site("Forum"), _site("FlakyForum", tentative=True))
        names = [s.name for s in cat.filtered()]
        assert names == ["Forum"]

    def test_knob_on_includes_tentative_sites(self, client: TestClient) -> None:
        admin_settings_mod.set_override("enable_tentative_sites", 1, actor="rootadmin")
        cat = _catalog(_site("Forum"), _site("FlakyForum", tentative=True))
        names = sorted(s.name for s in cat.filtered())
        assert names == ["FlakyForum", "Forum"]

    def test_explicit_tentative_tag_filter_overrides_off_knob(self, client: TestClient) -> None:
        # An operator who explicitly types ``tags=["tentative"]`` is
        # signalling intent: the tag filter wins over the knob's
        # default-off gate.
        cat = _catalog(_site("Forum"), _site("FlakyForum", tentative=True))
        names = [s.name for s in cat.filtered(tags=["tentative"])]
        assert names == ["FlakyForum"]

    def test_explicit_include_tentative_arg_overrides_knob(self, client: TestClient) -> None:
        cat = _catalog(_site("Forum"), _site("FlakyForum", tentative=True))
        # include_tentative=True forces inclusion regardless of knob.
        forced_on = [s.name for s in cat.filtered(include_tentative=True)]
        assert sorted(forced_on) == ["FlakyForum", "Forum"]
        # include_tentative=False forces exclusion regardless of knob.
        admin_settings_mod.set_override("enable_tentative_sites", 1, actor="rootadmin")
        forced_off = [s.name for s in cat.filtered(include_tentative=False)]
        assert forced_off == ["Forum"]


# ---------------------------------------------------------------------------
# checker._confidence — tentative-site CLAIMED cap
# ---------------------------------------------------------------------------


class TestConfidenceCapForTentativeSites:
    def test_default_cap_demotes_high_confidence_claim(self, client: TestClient) -> None:
        # Default cap is 70. A tentative-site message check with both
        # presence and absence absent (so base=70) stays at 70 — the
        # cap is a ceiling not a forcer.
        # Use a status_code site (base=80 for CLAIMED) and confirm
        # the 80 → 70 demotion happens.
        site = _site("FlakyForum", tentative=True)
        assert site.check_type == "status_code"
        c = _confidence(site, Status.CLAIMED, 200, [])
        # Default cap 70.
        assert c == 70

    def test_cap_only_applies_to_claimed_status(self, client: TestClient) -> None:
        site = _site("FlakyForum", tentative=True)
        # FREE / UNKNOWN / BLOCKED / ERROR rows are not capped — they
        # carry no false-claim risk and we want their full signal
        # surfaced.
        free = _confidence(site, Status.FREE, 404, ["absence-match"])
        assert free == 85  # status_code FREE-with-absence-match base
        unknown = _confidence(site, Status.UNKNOWN, 503, [])
        assert unknown == 25
        blocked = _confidence(site, Status.BLOCKED, 403, [])
        assert blocked == 40

    def test_non_tentative_sites_are_not_capped(self, client: TestClient) -> None:
        site = _site("Forum", tentative=False)
        c = _confidence(site, Status.CLAIMED, 200, [])
        assert c == 80  # status_code CLAIMED base, never capped

    def test_admin_can_lower_cap_below_precision_floor(self, client: TestClient) -> None:
        # Setting the cap below ``min_claimed_confidence`` (default
        # 60) deliberately makes every tentative-site CLAIMED demote
        # to UNKNOWN — a "look but never trust" configuration.
        admin_settings_mod.set_override("max_tentative_confidence", 50, actor="rootadmin")
        site = _site("FlakyForum", tentative=True)
        c = _confidence(site, Status.CLAIMED, 200, [])
        assert c == 50

    def test_cap_does_not_raise_low_confidence(self, client: TestClient) -> None:
        # Cap is a ceiling, not a floor. A tentative site whose base
        # confidence is already below the cap stays where it was.
        admin_settings_mod.set_override("max_tentative_confidence", 80, actor="rootadmin")
        site = _site("FlakyForum", tentative=True)
        # status_code CLAIMED base is 80, which is == cap; stays 80.
        assert _confidence(site, Status.CLAIMED, 200, []) == 80


# ---------------------------------------------------------------------------
# Knob round-trip via the admin API
# ---------------------------------------------------------------------------


class TestPhase10bKnobsRoundTrip:
    def test_describe_lists_phase_10b_knobs(self, client: TestClient) -> None:
        keys = {row["key"] for row in admin_settings_mod.describe()}
        assert "enable_tentative_sites" in keys
        assert "max_tentative_confidence" in keys

    def test_effective_map_returns_defaults(self, client: TestClient) -> None:
        m = admin_settings_mod.effective_map()
        assert m["enable_tentative_sites"] == 0
        assert m["max_tentative_confidence"] == 70

    def test_set_override_round_trips(self, client: TestClient) -> None:
        admin_settings_mod.set_override("enable_tentative_sites", 1, actor="rootadmin")
        admin_settings_mod.set_override("max_tentative_confidence", 55, actor="rootadmin")
        assert admin_settings_mod.effective_int("enable_tentative_sites") == 1
        assert admin_settings_mod.effective_int("max_tentative_confidence") == 55

    @pytest.mark.parametrize(
        "key,value",
        [
            ("enable_tentative_sites", -1),
            ("enable_tentative_sites", 2),
            ("max_tentative_confidence", -1),
            ("max_tentative_confidence", 101),
        ],
    )
    def test_set_override_rejects_out_of_range(
        self, client: TestClient, key: str, value: int
    ) -> None:
        with pytest.raises(admin_settings_mod.KnobOutOfRangeError):
            admin_settings_mod.set_override(key, value, actor="rootadmin")


# ---------------------------------------------------------------------------
# Loader integration — tentative flag survives a real load_catalog cycle
# ---------------------------------------------------------------------------


class TestLoaderRecognisesTentativeSites:
    def test_load_catalog_carries_tentative_through_real_loader(
        self, tmp_path, client: TestClient
    ) -> None:
        # Write a tiny sites.json containing one regular and one
        # tentative entry; load and confirm the loader honours both.
        import json

        path = tmp_path / "sites.json"
        path.write_text(
            json.dumps(
                {
                    "Forum": {
                        "url": "https://forum/{username}",
                        "urlMain": "https://forum/",
                        "checkType": "status_code",
                    },
                    "FlakyForum": {
                        "url": "https://flaky/{username}",
                        "urlMain": "https://flaky/",
                        "checkType": "status_code",
                        "tentative": True,
                        "tentativeReason": "returns_200_on_any_path",
                    },
                }
            )
        )
        # ``load_catalog`` uses an in-memory cache keyed on the path —
        # bust it here so the test body sees the tmp file.
        sites_mod._CACHED_CATALOG = None
        sites_mod._CACHED_BASE_PATH = None
        cat = sites_mod.load_catalog(path)
        regular = cat.by_name("Forum")
        flaky = cat.by_name("FlakyForum")
        assert regular is not None and regular.tentative is False
        assert flaky is not None and flaky.tentative is True
        assert flaky.tentative_reason == "returns_200_on_any_path"
        assert "tentative" in flaky.tags
        # Default knob (0) hides the tentative entry from the probe
        # set; flipping it on surfaces both.
        names_off = [s.name for s in cat.filtered()]
        assert names_off == ["Forum"]
        admin_settings_mod.set_override("enable_tentative_sites", 1, actor="rootadmin")
        names_on = sorted(s.name for s in cat.filtered())
        assert names_on == ["FlakyForum", "Forum"]
