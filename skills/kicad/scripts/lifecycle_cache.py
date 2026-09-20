#!/usr/bin/env python3
"""Cache, source ranking and confidence scoring for the lifecycle audit.

The audit used to be serial with a one-second sleep before every distributor
call. At four sources and 57 distinct MPNs that is 228 seconds of sleep before
a single packet moves, which is how a 600-second stage-8 budget came to be
exceeded by a board that had merely grown from 91 parts to 109.

Three things live here, and they are separate from the query functions on
purpose: this module makes no network calls and so can be tested without one.

  LifecycleCache   Distributor answers, keyed by (mpn, source), with a long
                   TTL. Lifecycle status moves on a scale of quarters, not
                   minutes, so re-running the pipeline during a design session
                   should cost nothing at all.

  score            How much to believe the answer. Four sources agreeing is
                   the ideal; the interesting cases are the mixtures.

  SourceScheduler  Which source to ask first and how long to wait, learned
                   from what each one actually did last time.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Iterable

__all__ = [
    "LifecycleCache",
    "SourceScheduler",
    "score",
    "STATUS_AXIS",
    "DEFAULT_TTL_DAYS",
]

# A month was the ask; a quarter is closer to how fast this data actually
# moves, so the default sits between and the CLI can widen it.
DEFAULT_TTL_DAYS = 45

# Lifecycle placed on one axis, so "how much do these sources disagree" has a
# magnitude rather than only a yes or no. active-vs-nrnd is a quibble;
# active-vs-obsolete means one of them is wrong about whether you can buy it.
STATUS_AXIS: dict[str, int] = {
    "active": 0,
    "nrnd": 1,
    "last_time_buy": 2,
    "discontinued": 3,
    "obsolete": 4,
}

# Confidence available from n sources that agree completely. Two agreeing
# distributors clear the 0.80 bar that lets a part be reported as settled;
# one source on its own never does, however emphatic it is.
_BASE_BY_COUNT: dict[int, float] = {0: 0.0, 1: 0.55, 2: 0.82, 3: 0.93, 4: 1.0}

# Weight on disagreement. At 0.5 a maximal split — active against obsolete —
# halves the confidence rather than erasing it, because "two distributors
# disagree" is itself a finding worth surfacing, not an absence of data.
_SPREAD_WEIGHT = 0.5


def score(per_source_status: dict[str, str],
          capable: int | None = None) -> dict[str, Any]:
    """Confidence in a part's lifecycle status, and why.

    Returns the agreed status where there is one, a confidence in [0, 1], and
    the components that produced it, so a reviewer can see whether a low score
    means "nobody answered" or "they answered and disagreed" — which look
    identical in a single number and call for opposite responses.
    """
    known = {s: st for s, st in (per_source_status or {}).items()
             if st in STATUS_AXIS}
    n = len(known)
    if n == 0:
        return {
            "confidence": 0.0,
            "status": "unknown",
            "responding": 0,
            "capable": capable,
            "agreement": None,
            "spread": None,
            "reason": "no source returned a usable status",
        }

    positions = [STATUS_AXIS[st] for st in known.values()]
    lo, hi = min(positions), max(positions)
    spread = (hi - lo) / (len(STATUS_AXIS) - 1)
    base = _BASE_BY_COUNT.get(min(n, 4), 1.0)
    confidence = base * (1.0 - _SPREAD_WEIGHT * spread)

    # The worst status anyone reports is the one that matters: a part one
    # distributor calls obsolete is a procurement problem even while another
    # still lists it.
    worst = max(known.values(), key=lambda st: STATUS_AXIS[st])
    # How many sources *could* have answered, when the caller knows. Two of
    # the four distributors here return stock and price but no lifecycle
    # field at all, so "one of four responded" reads as a failure when the
    # truth is "one of the two that can answer did".
    denom = ""
    if capable:
        denom = " of %d able to" % capable
    return {
        "confidence": round(confidence, 3),
        "status": worst,
        "responding": n,
        "capable": capable,
        "agreement": round(1.0 - spread, 3),
        "spread": round(spread, 3),
        "reason": (
            "%d source%s%s agreed" % (n, "" if n == 1 else "s", denom) if spread == 0
            else "%d sources, worst disagreement %d step%s on the lifecycle axis"
                 % (n, hi - lo, "" if hi - lo == 1 else "s")
        ),
    }


class LifecycleCache:
    """Distributor answers and per-source timing, persisted between runs.

    Read once at the start of a run and written once at the end, rather than
    locked and updated per entry: the workers are threads sharing one process,
    and a cache that needs a lock is a cache that can deadlock a build.
    """

    def __init__(self, path: str, ttl_days: float = DEFAULT_TTL_DAYS) -> None:
        self.path = path
        self.ttl = ttl_days * 86400.0
        self._entries: dict[str, dict] = {}
        self._timing: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0
        self.stale = 0
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict):
            return
        self._entries = blob.get("entries") or {}
        self._timing = blob.get("timing") or {}

    def save(self) -> None:
        blob = {
            "schema": 1,
            "saved_at": time.time(),
            "entries": self._entries,
            "timing": self._timing,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        # Write beside the target and rename, so an interrupted run cannot
        # leave a half-written cache that the next one fails to parse.
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(self.path)) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(blob, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- answers -----------------------------------------------------------

    @staticmethod
    def _key(mpn: str, source: str) -> str:
        return "%s\x1f%s" % (mpn.strip().upper(), source)

    def get(self, mpn: str, source: str) -> dict | None:
        row = self._entries.get(self._key(mpn, source))
        if row is None:
            self.misses += 1
            return None
        age = time.time() - row.get("fetched_at", 0)
        if age > self.ttl:
            self.stale += 1
            return None
        self.hits += 1
        return row.get("data")

    def put(self, mpn: str, source: str, data: dict | None) -> None:
        # A negative answer is cached too. A source that does not carry a part
        # will still not carry it tomorrow, and re-asking is the expensive
        # half of this audit.
        self._entries[self._key(mpn, source)] = {
            "fetched_at": time.time(),
            "data": data,
        }

    def covered(self, mpn: str, sources: Iterable[str],
                count: bool = True) -> dict[str, dict | None]:
        """Every fresh cached answer for one part.

        Counts hits by default so the run summary reflects what the cache
        actually saved. Callers that are only *probing* coverage — sizing a
        runtime estimate, say — pass count=False, since asking whether an
        answer exists is not the same as using it and inflating the hit rate
        would make the cache look better than it is.
        """
        out: dict[str, dict | None] = {}
        for src in sources:
            row = self._entries.get(self._key(mpn, src))
            if row is None:
                if count:
                    self.misses += 1
                continue
            if time.time() - row.get("fetched_at", 0) > self.ttl:
                if count:
                    self.stale += 1
                continue
            if count:
                self.hits += 1
            out[src] = row.get("data")
        return out

    # -- timing ------------------------------------------------------------

    def observe(self, source: str, seconds: float, ok: bool) -> None:
        row = self._timing.setdefault(
            source, {"ewma_s": None, "ok": 0, "timeouts": 0})
        if ok:
            row["ok"] += 1
            prev = row.get("ewma_s")
            # Recent behaviour matters more than history: a distributor that
            # slowed down this week should be reranked this week.
            row["ewma_s"] = seconds if prev is None else 0.7 * prev + 0.3 * seconds
        else:
            row["timeouts"] += 1

    def timing(self, source: str) -> dict:
        return dict(self._timing.get(source) or {"ewma_s": None, "ok": 0, "timeouts": 0})

    @property
    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "stale": self.stale,
                "entries": len(self._entries)}


# Timeout ladder. A source is asked with a small budget first and earns more
# only by needing it, so one unresponsive distributor costs two seconds per
# part on the first run rather than ten.
LADDER: tuple[float, ...] = (2.0, 4.0, 6.0, 8.0, 10.0)


class SourceScheduler:
    """Which source to ask first, and how long to give it.

    Ranking matters less than it would for serial queries, since the sources
    are asked concurrently. It earns its place in two other ways: the fastest
    sources are the ones an early exit gets to keep, and knowing each source's
    typical latency is what makes a runtime estimate possible at all — which
    is what lets stage 8 size its own timeout instead of guessing.
    """

    def __init__(self, cache: LifecycleCache, sources: list[str]) -> None:
        self.cache = cache
        self.sources = list(sources)

    def budget(self, source: str) -> float:
        """Seconds to allow this source, from how it behaved before."""
        t = self.cache.timing(source)
        ewma, timeouts, ok = t.get("ewma_s"), t.get("timeouts", 0), t.get("ok", 0)
        if ewma is None:
            # Never seen it answer. Start at the bottom of the ladder; a source
            # that genuinely needs longer will climb on its own.
            return LADDER[0] if timeouts == 0 else LADDER[min(timeouts, len(LADDER) - 1)]
        # Two and a half times the running mean, rounded up the ladder, so a
        # source sits one comfortable step above its own typical answer.
        want = ewma * 2.5
        for rung in LADDER:
            if want <= rung:
                return rung
        return LADDER[-1]

    def order(self) -> list[str]:
        """Sources fastest-first; unproven ones ahead of known-slow ones."""
        def sort_key(src: str) -> tuple[float, str]:
            t = self.cache.timing(src)
            ewma = t.get("ewma_s")
            if ewma is None:
                # Unproven sits mid-ladder: worth trying before a source known
                # to be slow, not before one known to be fast.
                return (LADDER[1], src)
            penalty = 1.0 + min(t.get("timeouts", 0), 5) * 0.5
            return (ewma * penalty, src)
        return sorted(self.sources, key=sort_key)

    def estimate(self, n_parts: int, concurrency: int,
                 cached_parts: int = 0) -> dict:
        """Roughly how long a run will take, for sizing the caller's timeout.

        Deliberately an estimate of the *work*, not a promise. The caller is
        expected to double it before using it as a deadline, because the thing
        this protects against is the anomaly, not the average.
        """
        todo = max(0, n_parts - cached_parts)
        if todo == 0:
            return {"seconds": 0.0, "parts_to_fetch": 0, "per_part_s": 0.0,
                    "concurrency": concurrency, "basis": "everything cached"}
        per_source = []
        for src in self.sources:
            t = self.cache.timing(src)
            per_source.append(t.get("ewma_s") or self.budget(src))
        # Sources run together for one part, so that part costs the slowest of
        # them; parts run together too, so the wall clock divides by workers.
        per_part = max(per_source) if per_source else LADDER[0]
        seconds = per_part * todo / max(1, concurrency)
        return {
            "seconds": round(seconds, 1),
            "parts_to_fetch": todo,
            "per_part_s": round(per_part, 2),
            "concurrency": concurrency,
            "basis": "measured" if any(
                self.cache.timing(s).get("ewma_s") for s in self.sources
            ) else "ladder defaults, no timings recorded yet",
        }


class RateLimiter:
    """A minimum interval between calls to the same distributor.

    The serial audit had ``time.sleep(1.0)`` before every call. That sleep was
    the audit's dominant cost and removing it is most of the speed-up — but it
    was also, accidentally, the only rate limiting there was. Firing eight
    parts at four sources concurrently put element14 straight into
    "Account Over Queries Per Second Limit", which is a 403 that looks exactly
    like a credential failure in a log and is not one.

    So the interval comes back, per source rather than globally, and it is
    learned: a source that never rejects us is never slowed down, and one that
    does gets backed off until it stops.
    """

    # Seeded from observed behaviour. element14 rejects at roughly three
    # requests per second; the others have not complained.
    DEFAULT_INTERVALS: dict[str, float] = {"element14": 1.1}

    MAX_INTERVAL = 8.0

    def __init__(self, cache: "LifecycleCache | None" = None) -> None:
        import threading
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self._interval: dict[str, float] = dict(self.DEFAULT_INTERVALS)
        self._cache = cache
        if cache is not None:
            for src, row in (cache._timing or {}).items():
                learned = row.get("min_interval_s")
                if learned:
                    self._interval[src] = float(learned)

    def interval(self, source: str) -> float:
        return self._interval.get(source, 0.0)

    def acquire(self, source: str) -> None:
        """Block until this source may be called again."""
        gap = self._interval.get(source, 0.0)
        if gap <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                ready = self._next_allowed.get(source, 0.0)
                if now >= ready:
                    self._next_allowed[source] = now + gap
                    return
                wait = ready - now
            time.sleep(min(wait, gap))

    def penalise(self, source: str) -> float:
        """A source said no. Back off, and remember it for the next run."""
        with self._lock:
            cur = self._interval.get(source, 0.0)
            new = min(self.MAX_INTERVAL, cur * 2 if cur > 0 else 1.0)
            self._interval[source] = new
        if self._cache is not None:
            row = self._cache._timing.setdefault(
                source, {"ewma_s": None, "ok": 0, "timeouts": 0})
            row["min_interval_s"] = new
            row["rejections"] = row.get("rejections", 0) + 1
        return new

    @staticmethod
    def is_rejection(exc: BaseException) -> bool:
        """Whether a failure was the source refusing us rather than breaking.

        A 429 is unambiguous. A 403 is not — it is also what an invalid key
        returns — so the body is checked for the words distributors actually
        use, and anything else is left alone rather than being silently
        treated as a rate limit that more waiting would cure.
        """
        code = getattr(exc, "code", None)
        if code == 429:
            return True
        if code != 403:
            return False
        try:
            body = exc.read().decode("utf-8", "replace").lower()  # type: ignore[attr-defined]
        except Exception:
            return False
        return any(t in body for t in
                   ("per second", "rate limit", "too many", "quota", "throttl"))


# ---------------------------------------------------------------------------
# Computed confidence
# ---------------------------------------------------------------------------

# The raw score answers "how much do the sources agree", and its ceiling is
# set by how many sources exist. That is honest but unhelpful on its own: a
# board where only one distributor publishes lifecycle at all can never exceed
# 0.55 raw, which reads as a failing grade for a part that is in fact as
# well-established as the available data allows.
#
# The computed score rescales the raw one against what was *achievable*. One
# capable source in full agreement is 80 of 100 - good, and explicitly not
# perfect, because a single opinion is a single opinion. Each further API
# source raises the ceiling by 5, and a human who went and looked raises it by
# 10, since a person reading a manufacturer's product page is better evidence
# than an API that does not carry the field at all.
_COMPUTED_BASE = 80.0
_PER_EXTRA_API = 5.0
_USER_BONUS = 10.0

# Hand-entered data goes stale. It is not wrong on a schedule, but the older
# it is the less it should carry, and past a year it should prompt someone to
# look again rather than quietly holding a board's score up.
_STALE_6MO_PENALTY = 5.0
_STALE_1YR_PENALTY = 10.0
_SIX_MONTHS = 182.5 * 86400
_ONE_YEAR = 365.0 * 86400

_RAW_CEILING_BY_COUNT = {1: 0.55, 2: 0.82, 3: 0.93, 4: 1.0}


def user_age_penalty(checked_at: float | None, now: float | None = None) -> tuple[float, str]:
    """How much to discount hand-entered data for age, and why."""
    if not checked_at:
        return _STALE_1YR_PENALTY, "no checked-on date"
    age = (now or time.time()) - checked_at
    if age > _ONE_YEAR:
        return _STALE_1YR_PENALTY, "checked over a year ago"
    if age > _SIX_MONTHS:
        return _STALE_6MO_PENALTY, "checked over six months ago"
    return 0.0, "checked within six months"


def compute(per_source_status: dict[str, str], api_capable: int,
            user: dict | None = None, now: float | None = None) -> dict:
    """Rescale agreement onto 0-100, and say what a human must do.

    Takes the raw per-source statuses rather than a finished raw score, because
    a hand-entered opinion has to be folded in *before* the agreement is
    measured. Scoring the APIs alone and then widening the divisor to admit the
    user made a corroborating entry lower the result than no entry at all,
    which is precisely backwards.

    ``user`` is a row a person filled in: ``{"status": ..., "checked_at":
    epoch, "reference": url}``. It counts as a capable source and is worth
    more than an API, but it is flagged rather than trusted silently — on a
    multi-user project someone else has to be able to repeat the check, which
    is why a reference is required for it to count at all.

    Nothing here blocks fabrication. Missing data and stale data both land in
    the same place: a part a human has to acknowledge before the board goes
    out, which is a decision someone makes on the record rather than a gate
    that a script decides it has the standing to close.
    """
    reasons: list[str] = []
    user_status = ((user or {}).get("status") or "").strip().lower()
    has_ref = bool((user or {}).get("reference"))
    # A recorded "unknown" is a real and useful thing to write down - someone
    # went to the vendor and there was nothing to find - but it is not an
    # opinion, so it must not raise the ceiling as though a source had voted.
    # Only a status that lands on the lifecycle axis counts.
    user_counts = bool(user_status in STATUS_AXIS and has_ref)
    if user_status and user_status not in STATUS_AXIS:
        reasons.append("human checked and found no lifecycle information "
                       "published (%s)" % (user.get("reference") or "no reference"))
    elif user_status and not has_ref:
        reasons.append("user-provided status ignored: no reference given, so "
                       "nobody else can repeat the check")

    combined = dict(per_source_status or {})
    if user_counts:
        combined["user"] = user_status

    raw = score(combined, capable=max(1, api_capable) + (1 if user_counts else 0))
    raw_conf = float(raw.get("confidence") or 0.0)
    responding = int(raw.get("responding") or 0)

    capable = max(1, api_capable) + (1 if user_counts else 0)
    ceiling = (_COMPUTED_BASE + _PER_EXTRA_API * max(0, api_capable - 1)
               + (_USER_BONUS if user_counts else 0.0))

    if responding == 0:
        return {
            "computed": 0.0, "ceiling": round(ceiling, 1), "raw": raw_conf,
            "status": "unknown", "capable": capable, "responding": 0,
            "user_counted": user_counts, "needs_ack": True, "blocks_fab": False,
            "reasons": reasons + ["no source could supply a lifecycle status"],
        }

    # Normalise against what was *achievable*, not against what answered.
    # Dividing by the ceiling for the number that responded made any single
    # agreeing source score full marks — one human check on a part reading 95
    # of 95, the same as that human plus two distributors all agreeing. The
    # ceiling is the maximum possible; reaching it has to require actually
    # getting the corroboration, not merely being able to ask for it.
    capable = max(capable, responding)
    raw_ceiling = _RAW_CEILING_BY_COUNT.get(min(capable, 4), 1.0)
    computed = (min(raw_conf, raw_ceiling) / raw_ceiling) * ceiling

    penalty = 0.0
    if user_counts:
        penalty, why = user_age_penalty(user.get("checked_at"), now)
        if penalty:
            reasons.append("user data discounted %.0f: %s" % (penalty, why))
    computed = max(0.0, computed - penalty)

    disagreement = (raw.get("spread") or 0) > 0
    if disagreement:
        reasons.append("sources disagree — %s" % raw.get("reason", ""))

    needs_ack = bool(penalty) or disagreement or computed < _COMPUTED_BASE
    return {
        "computed": round(computed, 1),
        "ceiling": round(ceiling, 1),
        "raw": raw_conf,
        "status": raw.get("status", "unknown"),
        "capable": capable,
        "responding": responding,
        "user_counted": user_counts,
        "needs_ack": needs_ack,
        # Lifecycle data that cannot be had is not a reason to stop a board.
        # It is a reason for someone to say, in writing, that they know.
        "blocks_fab": False,
        "reasons": reasons,
    }
