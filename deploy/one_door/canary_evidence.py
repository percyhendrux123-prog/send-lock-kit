"""Offline scanner over trusted observer records; never collects data or dispatches tools."""
from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass, field
from math import isfinite
import re
from secrets import token_hex
from urllib.parse import unquote_to_bytes


TOKEN = re.compile(r'SLKCANARY-[0-9a-f]{32}')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')


def _label(value):
    return isinstance(value, str) and LABEL.fullmatch(value) is not None


def _instant(value):
    return type(value) is int or (type(value) is float and isfinite(value))


def _labels(values):
    if not isinstance(values, (list, tuple, set, frozenset)) or not all(map(_label, values)):
        raise ValueError('invalid observer labels')
    return frozenset(values)


@dataclass(frozen=True)
class Canary:
    canary_id: str
    value: str = field(repr=False)
    origin: str
    start: int | float
    end: int | float
    permitted: frozenset[str] = frozenset()
    # None means sealed: every non-origin destination is forbidden.
    forbidden: frozenset[str] | None = None

    def __post_init__(self):
        if not _label(self.canary_id) or not _label(self.origin):
            raise ValueError('invalid canary ID or origin')
        if not isinstance(self.value, str) or not TOKEN.fullmatch(self.value):
            raise ValueError('canary is not a synthetic marker')
        if not (_instant(self.start) and _instant(self.end) and self.start < self.end):
            raise ValueError('canary window must have finite, increasing bounds')
        permitted = _labels(self.permitted)
        object.__setattr__(self, 'permitted', permitted)
        if self.forbidden is None:
            if permitted:
                raise ValueError('sealed canaries may not have permitted copies')
        else:
            forbidden = _labels(self.forbidden)
            if not forbidden or forbidden & (permitted | {self.origin}):
                raise ValueError('forbidden destinations must be nonempty and disjoint from permitted ones')
            object.__setattr__(self, 'forbidden', forbidden)

    @classmethod
    def mint(cls, canary_id, origin, start, end, *, permitted=frozenset(), forbidden=None):
        return cls(canary_id, 'SLKCANARY-' + token_hex(16), origin, start, end, permitted, forbidden)

    def active(self, row):
        return self.start <= row.at < self.end

    def forbids(self, observer):
        if self.forbidden is None:
            return observer != self.origin
        return observer in self.forbidden


@dataclass(frozen=True)
class Observation:
    observer: str
    at: int | float
    body: bytes = field(repr=False)

    def __post_init__(self):
        if not _label(self.observer) or not _instant(self.at) or not isinstance(self.body, bytes):
            raise ValueError('expected an observer label, finite timestamp and readable bytes')


@dataclass(frozen=True)
class Arrival:
    canary_id: str
    observer: str
    at: int | float
    encoding: str


def _unique(registry):
    registry = tuple(registry)
    if len({c.canary_id for c in registry}) != len(registry) or len({c.value for c in registry}) != len(registry):
        raise ValueError('canary IDs and values may not be reused')
    return registry


def scan(registry, observations):
    """Return one match per forbidden marker per record, never marker or record contents.

    Unlisted destinations are outside the scope of a reachable canary's scan;
    sealed canaries treat every observed non-origin as forbidden.
    """
    signatures = []
    for marker in _unique(registry):
        raw = marker.value.encode('ascii')
        signatures.append((marker, raw, raw.hex().encode('ascii'),
                           {b64encode(raw).rstrip(b'='), urlsafe_b64encode(raw).rstrip(b'=')}))
    hits = []
    for row in observations:
        eligible = [entry for entry in signatures if entry[0].active(row) and entry[0].forbids(row.observer)]
        if not eligible:
            continue
        raw_lower = row.body.lower()
        url_decoded = unquote_to_bytes(row.body)
        for marker, raw, hexed, b64 in eligible:
            if raw in row.body:
                form = 'literal'
            elif hexed in raw_lower:
                form = 'hex'
            elif any(variant in row.body for variant in b64):
                form = 'base64'
            elif raw in url_decoded:
                form = 'url'
            else:
                continue
            hits.append(Arrival(marker.canary_id, row.observer, row.at, form))
    return tuple(hits)


@dataclass(frozen=True)
class BaselineReport:
    missing_no_action: tuple[str, ...]
    unseen_origins: tuple[str, ...]
    unauthorized_no_action: tuple[Arrival, ...]
    failed_positives: tuple[str, ...]

    @property
    def passed(self):
        return not (self.missing_no_action or self.unseen_origins or self.unauthorized_no_action or self.failed_positives)


def check_baselines(required, registry, no_action, positives):
    """Score operator-supplied quiet records and observer-specific positive fixtures.

    `positives` maps each observer to (fresh sealed canary, its isolated observations).
    An empty readable quiet record is allowed; a missing record is not evidence.
    """
    required = _labels(required)
    if not required or positives.keys() - required:
        raise ValueError('expected a nonempty observer set and only associated positive controls')
    registry = _unique(registry)
    _unique((*registry, *(marker for marker, _ in positives.values())))
    quiet = tuple(no_action)
    missing = tuple(name for name in sorted(required) if not registry or any(
        not any(row.observer == name and marker.active(row) for row in quiet) for marker in registry))
    unseen = tuple(marker.canary_id for marker in registry if not any(
        row.observer == marker.origin and marker.active(row) and marker.value.encode('ascii') in row.body
        for row in quiet))
    failed = []
    for name in sorted(required):
        if name not in positives:
            failed.append(name)
            continue
        marker, rows = positives[name]
        hits = scan([marker], rows) if marker.forbidden is None else ()
        if {hit.observer for hit in hits} != {name}:
            failed.append(name)
    return BaselineReport(missing, unseen, scan(registry, quiet), tuple(failed))
