"""Phase 5c: deterministic username permutation generator.

The OSINT operator types one base handle (``"torvalds"``); we fan out to
a small, predictable set of common variants — separator/affix/leetspeak
twists — and run the catalog scan against each. The output is intended
to be a *short* list (≤ ~40 by default); callers cap with
``max_count``.

Design constraints:

* **Deterministic, no randomness.** The same base + strategy set
  always produces the same ordered list. This matters for the diff
  view in scan history and for SSE caching keys.
* **Pure stdlib.** No external deps; the work is small enough that
  micro-optimisation isn't worth a dependency.
* **Safe inputs only.** Variants that don't match
  ``checker.is_valid_username`` (i.e., not ``[A-Za-z0-9_.-]{1,64}``)
  are dropped — never sent over the wire.
* **Stability over coverage.** When in doubt, generate fewer rather
  than more — the catalog has 2,500+ sites so even a tiny multiplier
  blows out latency.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .checker import is_valid_username


class UnknownStrategyError(ValueError):
    """Raised when a caller asks for a permutation strategy we don't ship."""


@dataclass(frozen=True)
class StrategySpec:
    """Static metadata for one permutation strategy."""

    key: str
    title: str
    description: str


STRATEGIES: tuple[StrategySpec, ...] = (
    StrategySpec(
        key="separator",
        title="Separators",
        description=(
            "Insert a single common separator (``_``, ``.``, ``-``) at "
            "word-boundary positions inferred from camelCase / digit "
            "transitions. ``JohnDoe2024`` → ``john_doe_2024`` etc."
        ),
    ),
    StrategySpec(
        key="suffix",
        title="Suffixes",
        description=(
            "Append common handle suffixes — single digits, year-style "
            "two-digit numbers, ``_real`` / ``_official`` / ``_yt`` / "
            "``_ig`` — to the base."
        ),
    ),
    StrategySpec(
        key="prefix",
        title="Prefixes",
        description=(
            "Prepend common handle prefixes — ``the_`` / ``real_`` / "
            "``official_`` / ``mr_`` / ``ms_`` — to the base."
        ),
    ),
    StrategySpec(
        key="leetspeak",
        title="Leetspeak",
        description=(
            "Replace lowercase vowels and ``s/t/g`` with their canonical "
            "leetspeak digit equivalents (``a→4``, ``e→3``, ``i→1``, "
            "``o→0``, ``s→5``, ``t→7``, ``g→9``). Limited to single-char "
            "swaps to keep the output set bounded."
        ),
    ),
)

STRATEGY_KEYS: frozenset[str] = frozenset(s.key for s in STRATEGIES)

# Default ordering when the caller asks for "all" strategies. Listed
# from "most likely to belong to the same person" → "noisier".
DEFAULT_STRATEGY_ORDER: tuple[str, ...] = (
    "separator",
    "suffix",
    "prefix",
    "leetspeak",
)

# Strategy seed lists. Public so admin tooling / tests can reference
# them without monkey-patching internals.
COMMON_SUFFIXES: tuple[str, ...] = (
    "_",
    "1",
    "01",
    "07",
    "23",
    "_real",
    "_official",
    "_yt",
    "_ig",
)

COMMON_PREFIXES: tuple[str, ...] = (
    "the_",
    "real_",
    "official_",
    "mr_",
    "ms_",
)

# Single-character leetspeak swaps — bounded so the output set doesn't
# explode combinatorially.
_LEET_SWAPS: tuple[tuple[str, str], ...] = (
    ("a", "4"),
    ("e", "3"),
    ("i", "1"),
    ("o", "0"),
    ("s", "5"),
    ("t", "7"),
    ("g", "9"),
)


def describe_strategies() -> list[dict[str, str]]:
    """Return the strategy registry as a list of plain dicts.

    Used by the public ``GET /api/settings`` and the frontend to render
    the permutation strategy checkboxes.
    """
    return [{"key": s.key, "title": s.title, "description": s.description} for s in STRATEGIES]


def _word_boundaries(s: str) -> list[int]:
    """Return positions in ``s`` where a separator could be inserted.

    Boundaries: lowercase→uppercase, alpha→digit, digit→alpha. Indexed
    such that ``s[:i] + sep + s[i:]`` produces a valid split. We never
    return 0 or ``len(s)`` (those would create leading/trailing
    separators, which most platforms reject anyway).
    """
    out: list[int] = []
    for i in range(1, len(s)):
        prev, curr = s[i - 1], s[i]
        if (
            (prev.islower() and curr.isupper())
            or (prev.isalpha() and curr.isdigit())
            or (prev.isdigit() and curr.isalpha())
        ):
            out.append(i)
    return out


def _separator_variants(base: str) -> list[str]:
    boundaries = _word_boundaries(base)
    out: list[str] = []
    seen: set[str] = set()
    # Single-separator inserts at the *first* boundary keep the variant
    # closest to the original; multi-boundary inserts apply *one* sep
    # at *all* boundaries (rare but extremely common in practice — see
    # camelCase handles like "JohnDoe" → "john_doe").
    for sep in ("_", ".", "-"):
        for i in boundaries:
            cand = (base[:i] + sep + base[i:]).lower()
            if cand not in seen:
                out.append(cand)
                seen.add(cand)
        if len(boundaries) > 1:
            parts: list[str] = []
            cursor = 0
            for i in boundaries:
                parts.append(base[cursor:i])
                cursor = i
            parts.append(base[cursor:])
            cand = sep.join(parts).lower()
            if cand not in seen:
                out.append(cand)
                seen.add(cand)
    return out


def _suffix_variants(base: str) -> list[str]:
    return [base + s for s in COMMON_SUFFIXES]


def _prefix_variants(base: str) -> list[str]:
    return [p + base for p in COMMON_PREFIXES]


def _leet_variants(base: str) -> list[str]:
    """Generate single-character leetspeak swaps over ``base``.

    For each (letter, digit) pair we replace **all** occurrences of
    that letter at once (e.g., ``"google"`` → ``"g00gle"`` not
    ``"g0ogle"`` and ``"go0gle"`` separately). This keeps the output
    bounded by ``len(_LEET_SWAPS)`` per base instead of exploding
    factorially. We then add a single all-swaps variant where every
    matching letter is replaced.
    """
    lower = base.lower()
    out: list[str] = []
    seen: set[str] = set()
    for letter, digit in _LEET_SWAPS:
        if letter in lower:
            cand = lower.replace(letter, digit)
            if cand != lower and cand not in seen:
                out.append(cand)
                seen.add(cand)
    # All-swap variant: most aggressive, listed last so cap-truncation
    # drops it first.
    aggressive = lower
    for letter, digit in _LEET_SWAPS:
        aggressive = aggressive.replace(letter, digit)
    if aggressive != lower and aggressive not in seen:
        out.append(aggressive)
    return out


_GENERATORS: dict[str, Callable[[str], list[str]]] = {
    "separator": _separator_variants,
    "suffix": _suffix_variants,
    "prefix": _prefix_variants,
    "leetspeak": _leet_variants,
}


def _normalize_strategies(strategies: Iterable[str] | None) -> list[str]:
    if strategies is None:
        return list(DEFAULT_STRATEGY_ORDER)
    seen: set[str] = set()
    out: list[str] = []
    for s in strategies:
        if s in seen:
            continue
        if s not in STRATEGY_KEYS:
            raise UnknownStrategyError(s)
        out.append(s)
        seen.add(s)
    return out or list(DEFAULT_STRATEGY_ORDER)


def generate_permutations(
    base: str,
    *,
    max_count: int = 40,
    strategies: Iterable[str] | None = None,
) -> list[str]:
    """Return up to ``max_count`` permutations of ``base``.

    The base itself is **not** included in the output — callers
    typically run a primary scan on it first. Variants that fail
    ``is_valid_username`` (out-of-range chars, too long after suffix,
    etc.) are silently dropped.

    Order is ``(strategy_order, generator_order)`` so the most
    "person-like" variants land first; the cap then truncates the
    noisier tail.
    """
    if max_count <= 0:
        return []
    base = base.strip()
    if not is_valid_username(base):
        raise ValueError("invalid base username")
    chosen = _normalize_strategies(strategies)

    seen: set[str] = {base.lower()}  # never re-emit the base
    out: list[str] = []
    for key in chosen:
        for cand in _GENERATORS[key](base):
            cand = cand.strip()
            cand_key = cand.lower()
            if cand_key in seen:
                continue
            if not is_valid_username(cand):
                continue
            out.append(cand)
            seen.add(cand_key)
            if len(out) >= max_count:
                return out
    return out
