#!/usr/bin/env python3
"""Component lifecycle and temperature audit.

Reads analyzer JSON output (BOM section) and queries distributor APIs for
lifecycle status and operating temperature data. Flags obsolete, NRND, and
EOL components, and checks temperature range coverage against a design target.

This is a standalone script (not part of the analyzer) because it requires
network access for distributor API queries. The analyzer must remain
zero-dependency and offline.

Usage:
    python3 lifecycle_audit.py analysis.json
    python3 lifecycle_audit.py analysis.json --temp-range "industrial"
    python3 lifecycle_audit.py analysis.json --temp-range="-40,85"  # use = for negative min
    python3 lifecycle_audit.py analysis.json --output lifecycle.json
    python3 lifecycle_audit.py analysis.json --only digikey

Environment:
    DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET — DigiKey OAuth 2.0
    MOUSER_SEARCH_API_KEY — Mouser API key
    ELEMENT14_API_KEY — element14/Newark API key
    (LCSC requires no credentials)
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Temperature presets
# ---------------------------------------------------------------------------

_TEMP_PRESETS = {
    "commercial": (0, 70),
    "industrial": (-40, 85),
    "extended": (-40, 105),
    "automotive": (-40, 125),
    "military": (-55, 125),
}

_LIFECYCLE_STATUS_RULES = {
    'obsolete': ('LC-001', 'error'),
    'discontinued': ('LC-001', 'error'),
    'last_time_buy': ('LC-002', 'warning'),
    'nrnd': ('LC-003', 'warning'),
    'unknown': ('LC-004', 'info'),
}


def _classify_temp_grade(temp_min: float, temp_max: float) -> str:
    """Classify a temperature range into an industry grade."""
    if temp_min <= -55 and temp_max >= 125:
        return "military"
    if temp_min <= -40 and temp_max >= 125:
        return "automotive"
    if temp_min <= -40 and temp_max >= 105:
        return "extended"
    if temp_min <= -40 and temp_max >= 85:
        return "industrial"
    if temp_min <= 0 and temp_max >= 70:
        return "commercial"
    return "non-standard"


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

_STATUS_NORMALIZE = {
    # DigiKey ProductStatus.Status values
    "active": "active",
    "active, not stocked": "active",
    "discontinued": "discontinued",
    "last time buy": "last_time_buy",
    "not for new designs": "nrnd",
    "obsolete": "obsolete",
    # Mouser
    "new product": "active",
    "end of life": "obsolete",
    "factory special order": "active",
    # Nexar / Octopart. Their value carries a freshness suffix - "Production
    # (Last Updated: 2 weeks ago)" - which the normaliser strips before lookup.
    "production": "active",
    "new product": "active",
    "not recommended for new designs": "nrnd",
    "end of life": "obsolete",
    "obsolete": "obsolete",
    "last time buy": "last_time_buy",
    # Generic
    "nrnd": "nrnd",
    "eol": "obsolete",
    "ltb": "last_time_buy",
}


def _normalize_status(raw: str | None) -> str:
    """Normalize a lifecycle status string to a standard value.

    Sources decorate the word. Nexar returns "Production (Last Updated: 2
    weeks ago)", which carries useful provenance and no extra meaning, so the
    parenthetical is dropped before the lookup rather than turning a perfectly
    good status into "unknown".
    """
    if not raw:
        return "unknown"
    text = raw.lower().strip()
    text = re.sub(r"\s*\(.*?\)\s*$", "", text).strip()
    return _STATUS_NORMALIZE.get(text, "unknown")


# ---------------------------------------------------------------------------
# Temperature parsing
# ---------------------------------------------------------------------------

def _parse_temp_range(text: str) -> tuple[float, float] | None:
    """Parse temperature range from distributor attribute string.

    Examples: "-40°C ~ 85°C", "-40C to +125C", "-40°C~+85°C", "0 ~ 70"
    """
    if not text:
        return None
    m = re.search(r'(-?\d+)\s*°?\s*C?\s*[~\-–—to]+\s*\+?(-?\d+)\s*°?\s*C?', text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


# ---------------------------------------------------------------------------
# MPN filtering (same pattern as sync_datasheets_digikey.py)
# ---------------------------------------------------------------------------

_GENERIC_VALUE_RE = re.compile(
    r"^[\d.]+\s*[pnuμmkMGR]?[FHΩRfhω]?$"
    r"|^[\d.]+\s*[kKmM]?[Ωω]?$"
    r"|^[\d.]+\s*[pnuμm]?[Ff]$"
    r"|^[\d.]+\s*[pnuμm]?[Hh]$"
    r"|^[\d.]+%$"
    r"|^DNP$|^NC$|^N/?A$",
    re.IGNORECASE,
)

_SKIP_TYPES = {
    "test_point", "mounting_hole", "fiducial", "graphic",
    "jumper", "net_tie", "mechanical",
}


def _is_real_mpn(mpn: str) -> bool:
    if not mpn or len(mpn) < 3:
        return False
    if _GENERIC_VALUE_RE.match(mpn.strip()):
        return False
    has_letter = any(c.isalpha() for c in mpn)
    has_digit = any(c.isdigit() for c in mpn)
    return has_letter and has_digit


# ---------------------------------------------------------------------------
# DigiKey API (OAuth 2.0)
# ---------------------------------------------------------------------------

def _get_digikey_token() -> tuple[str, str] | None:
    client_id = os.environ.get("DIGIKEY_CLIENT_ID", "")
    client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    # Token cache
    cache_path = os.path.join(tempfile.gettempdir(), "digikey_token_cache.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        if cache.get("expires_at", 0) > time.time():
            return cache["access_token"], client_id
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    try:
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }).encode()
        req = urllib.request.Request(
            "https://api.digikey.com/v1/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read())
        token = token_data["access_token"]
        with open(cache_path, "w") as f:
            json.dump({"access_token": token, "expires_at": time.time() + 540}, f)
        return token, client_id
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return None


def query_lifecycle_digikey(mpn: str, timeout: float = 10.0) -> dict | None:
    """Query DigiKey for lifecycle and temperature data."""
    auth = _get_digikey_token()
    if not auth:
        return None
    token, client_id = auth

    try:
        body = json.dumps({"Keywords": mpn, "Limit": 3}).encode()
        req = urllib.request.Request(
            "https://api.digikey.com/products/v4/search/keyword",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    for product in data.get("Products", []):
        prod_mpn = product.get("ManufacturerProductNumber", "")
        if not prod_mpn.upper().startswith(mpn.upper()[:6]):
            continue

        result = {}

        # Lifecycle status
        status = product.get("ProductStatus", {})
        if isinstance(status, dict):
            result["status"] = status.get("Status")
        elif isinstance(status, str):
            result["status"] = status
        result["discontinued"] = product.get("Discontinued", False)

        # Operating temperature from parameters
        for param in product.get("Parameters", []):
            ptext = param.get("ParameterText", "").lower()
            pval = param.get("ValueText", "")
            if "operating temperature" in ptext and pval:
                temp = _parse_temp_range(pval)
                if temp:
                    result["temp_min_c"] = temp[0]
                    result["temp_max_c"] = temp[1]
                    result["temp_raw"] = pval
                break

        return result
    return None


# ---------------------------------------------------------------------------
# Mouser API
# ---------------------------------------------------------------------------

def query_lifecycle_mouser(mpn: str, timeout: float = 10.0) -> dict | None:
    """Query Mouser for lifecycle and temperature data."""
    api_key = os.environ.get("MOUSER_SEARCH_API_KEY") or os.environ.get("MOUSER_PART_API_KEY")
    if not api_key:
        return None

    try:
        body = json.dumps({
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "",
            }
        }).encode()
        url = f"https://api.mouser.com/api/v1/search/partnumber?apiKey={api_key}"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    for part in data.get("SearchResults", {}).get("Parts", []):
        result = {}
        result["status"] = part.get("LifecycleStatus")
        # On the tier this key reaches, LifecycleStatus and ProductStatus come
        # back null on every part while Availability and pricing are populated.
        # Saying so is better than letting a null masquerade as a silent
        # source that might have agreed with DigiKey: the confidence model
        # should know this source cannot vote, not think it abstained.
        if not result["status"]:
            result["provides_status"] = False
            avail = part.get("Availability") or ""
            if avail:
                result["availability"] = avail
        result["discontinued"] = str(part.get("IsDiscontinued", "")).lower() == "true"
        result["lead_time"] = part.get("LeadTime")
        result["suggested_replacement"] = part.get("SuggestedReplacement")

        for attr in part.get("ProductAttributes", []):
            aname = attr.get("AttributeName", "").lower()
            aval = attr.get("AttributeValue", "")
            if "operating temperature" in aname and aval:
                temp = _parse_temp_range(aval)
                if temp:
                    result["temp_min_c"] = temp[0]
                    result["temp_max_c"] = temp[1]
                    result["temp_raw"] = aval
                break

        return result
    return None


# ---------------------------------------------------------------------------
# LCSC (no auth)
# ---------------------------------------------------------------------------

def query_lifecycle_lcsc(mpn: str, timeout: float = 10.0) -> dict | None:
    """Query LCSC for availability and temperature data."""
    try:
        url = f"https://jlcsearch.tscircuit.com/api/search?q={urllib.parse.quote(mpn)}&limit=3&full=true"
        req = urllib.request.Request(url, headers={"User-Agent": "kicad-happy-lifecycle/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    # The upstream response used to nest a part under "extra" with its own
    # mpn and attributes. It now returns the fields flat — description, mfr,
    # lcsc, package, price, stock — so the old reader found no "extra", took
    # comp_mpn as empty, skipped every component and returned None for every
    # part on the board. It failed silently, which is the worst way for a
    # source to fail: the audit reported a clean run with one fewer opinion in
    # it and nothing said so.
    needle = mpn.upper()[:6]
    for comp in data.get("components", []):
        extra = comp.get("extra") or {}
        haystack = " ".join(str(comp.get(k) or "") for k in
                            ("mfr", "description", "lcsc")).upper()
        comp_mpn = (extra.get("mpn") or comp.get("mfr") or "").upper()
        if needle not in haystack and not comp_mpn.startswith(needle):
            continue

        result = {}
        stock = comp.get("stock", 0) or 0
        result["in_stock"] = stock > 0
        result["stock_qty"] = stock
        # No lifecycle status is available from this source; it speaks to
        # stock and temperature only. Saying so explicitly keeps it out of the
        # confidence model's numerator rather than looking like a silent
        # source that might have agreed.
        result["provides_status"] = False

        attrs = extra.get("attributes") or {}
        for k, v in attrs.items():
            if "operating temperature" in k.lower() and v:
                temp = _parse_temp_range(v)
                if temp:
                    result["temp_min_c"] = temp[0]
                    result["temp_max_c"] = temp[1]
                    result["temp_raw"] = v
                break

        return result
    return None


# ---------------------------------------------------------------------------
# element14 API
# ---------------------------------------------------------------------------

def query_lifecycle_element14(mpn: str, timeout: float = 10.0,
                              store: str | None = None) -> dict | None:
    """Query element14 for availability and temperature data.

    Rebuilt against the documented request shape on 2026-09-20. The previous
    version returned None for every part because it sent
    ``storeInfo.id=us.newark.com``, which is not a store element14 recognises —
    the identifiers are ``www.newark.com`` and ``uk.farnell.com`` — and omitted
    ``callInfo.responseDataFormat``. The API answered 400 to all of it, the
    caller swallowed the HTTPError as an ordinary URLError, and the audit
    recorded a silent abstention rather than a broken request.

    Note what this source does *not* return. ``productStatus`` holds values
    like STOCKED and DIRECT_SHIP, which describe how element14 fulfils an
    order rather than where the part sits in its life. Mapping those onto
    active/obsolete would be inventing a lifecycle opinion, so the result is
    marked as carrying no status and the confidence model leaves it out of the
    count rather than treating it as a source that stayed quiet.
    """
    api_key = os.environ.get("ELEMENT14_API_KEY")
    if not api_key:
        return None

    store = store or os.environ.get("ELEMENT14_STORE") or "www.newark.com"
    # Built by hand rather than with urlencode: the documented samples put
    # callInfo.apiKey last and include an empty refinements.filters, and this
    # is the shape that answers 200.
    qs = (
        "term=manuPartNum%%3A%s"
        "&storeInfo.id=%s"
        "&resultsSettings.offset=0"
        "&resultsSettings.numberOfResults=2"
        "&resultsSettings.refinements.filters="
        "&resultsSettings.responseGroup=inventory"
        "&callInfo.responseDataFormat=JSON"
        "&callInfo.apiKey=%s"
    ) % (urllib.parse.quote(mpn), store, urllib.parse.quote(api_key))

    try:
        req = urllib.request.Request("https://api.element14.com/catalog/products?" + qs,
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    products = (data.get("manufacturerPartNumberSearchReturn", {})
                    .get("products") or [])
    for product in products:
        result: dict = {"provides_status": False}
        fulfilment = product.get("productStatus")
        if fulfilment:
            result["fulfilment"] = fulfilment
        if product.get("sku"):
            result["sku"] = product["sku"]
        if product.get("isAwaitingRelease") is not None:
            result["awaiting_release"] = bool(product["isAwaitingRelease"])
        for attr in product.get("attributes") or []:
            label = (attr.get("attributeLabel") or "").lower()
            value = attr.get("attributeValue", "")
            if "operating temperature" in label and value:
                temp = _parse_temp_range(value)
                if temp:
                    result["temp_min_c"] = temp[0]
                    result["temp_max_c"] = temp[1]
                    result["temp_raw"] = value
        return result
    return None


# ---------------------------------------------------------------------------
# Nexar / Octopart
# ---------------------------------------------------------------------------

def _get_nexar_token() -> str | None:
    """A Nexar bearer token from the client-credentials pair.

    No redirect and no browser, so the callback URL the portal insists on at
    app creation is never exercised by this flow. A static NEXAR_ACCESS_TOKEN
    is honoured if that is all there is, but it expires in a day, so the
    credentials are preferred and the minted token is cached like DigiKey's.
    """
    cid = os.environ.get("NEXAR_CLIENT_ID")
    secret = os.environ.get("NEXAR_CLIENT_SECRET")
    if not (cid and secret):
        return os.environ.get("NEXAR_ACCESS_TOKEN") or None

    cache_path = os.path.join(tempfile.gettempdir(), "nexar_token_cache.json")
    try:
        with open(cache_path) as fh:
            cached = json.load(fh)
        if cached.get("expires_at", 0) > time.time() + 60:
            return cached["access_token"]
    except (OSError, ValueError, KeyError):
        pass

    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid, "client_secret": secret,
        "scope": "supply.domain",
    }).encode()
    try:
        req = urllib.request.Request(
            "https://identity.nexar.com/connect/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return None

    try:
        with open(cache_path, "w") as fh:
            json.dump({"access_token": tok["access_token"],
                       "expires_at": time.time() + int(tok.get("expires_in", 3600))}, fh)
    except OSError:
        pass
    return tok.get("access_token")


_NEXAR_QUERY = """
query ($q: String!) {
  supSearchMpn(q: $q, limit: 1) {
    results {
      part {
        mpn
        manufacturer { name }
        totalAvail
        estimatedFactoryLeadDays
        specs { attribute { shortname } value displayValue }
      }
    }
  }
}"""


def query_lifecycle_nexar(mpn: str, timeout: float = 10.0) -> dict | None:
    """Query Nexar for lifecycle, availability and temperature.

    Deliberately not in the default source set. An evaluation licence carries
    a hard lifetime cap on part lookups — a hundred, on the one this was built
    against — so a source that silently ran on every pipeline invocation would
    spend the whole allowance during a single afternoon's design iteration.
    It runs when asked for and not otherwise, and the cache means asking twice
    for the same part costs one lookup.

    Lifecycle arrives as a spec rather than a field: attribute shortname
    ``lifecyclestatus``, with values like "Production (Last Updated: 2 weeks
    ago)".
    """
    token = _get_nexar_token()
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "https://api.nexar.com/graphql",
            data=json.dumps({"query": _NEXAR_QUERY, "variables": {"q": mpn}}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    if data.get("errors"):
        return None

    results = (((data.get("data") or {}).get("supSearchMpn") or {})
               .get("results") or [])
    for entry in results:
        part = entry.get("part") or {}
        got = (part.get("mpn") or "").upper()
        if got and not got.startswith(mpn.upper()[:6]):
            continue
        result: dict = {}
        for spec in part.get("specs") or []:
            short = ((spec.get("attribute") or {}).get("shortname") or "").lower()
            value = spec.get("displayValue") or spec.get("value")
            if short == "lifecyclestatus" and value:
                result["status"] = value
            elif "operating" in short and "temp" in short and value:
                temp = _parse_temp_range(str(value))
                if temp:
                    result["temp_min_c"] = temp[0]
                    result["temp_max_c"] = temp[1]
                    result["temp_raw"] = value
        avail = part.get("totalAvail")
        if avail is not None:
            result["in_stock"] = avail > 0
            result["stock_qty"] = avail
        lead = part.get("estimatedFactoryLeadDays")
        if lead is not None:
            result["lead_time_days"] = lead
        if part.get("manufacturer"):
            result["manufacturer"] = (part["manufacturer"] or {}).get("name")
        if not result.get("status"):
            result["provides_status"] = False
        return result or None
    return None


# ---------------------------------------------------------------------------
# Datasheet extraction cache (local, no network)
# ---------------------------------------------------------------------------

def read_extraction_temperature(mpn: str, project_dir: str) -> dict | None:
    """Read temperature data from datasheet extraction cache."""
    if not project_dir:
        return None

    sanitized = re.sub(r'[^A-Za-z0-9_]', '_', mpn.strip())
    extract_path = Path(project_dir) / "datasheets" / "extracted" / f"{sanitized}.json"

    if not extract_path.exists():
        # Try index lookup
        extracted_dir = Path(project_dir) / "datasheets" / "extracted"
        idx_path = extracted_dir / "manifest.json"
        if not idx_path.exists():
            idx_path = extracted_dir / "index.json"
        if idx_path.exists():
            try:
                with open(idx_path) as f:
                    idx = json.load(f)
                for k, v in idx.get("extractions", {}).items():
                    if k.upper() == sanitized.upper():
                        extract_path = Path(project_dir) / "datasheets" / "extracted" / v.get("file", "")
                        break
            except (json.JSONDecodeError, OSError):
                pass
        if not extract_path.exists():
            return None

    try:
        with open(extract_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    ops = data.get("recommended_operating_conditions", {})
    temp_min = ops.get("temp_min_c")
    temp_max = ops.get("temp_max_c")
    if temp_min is not None and temp_max is not None:
        return {
            "temp_min_c": temp_min,
            "temp_max_c": temp_max,
            "source": "extraction_cache",
        }
    return None


# ---------------------------------------------------------------------------
# Per-component audit
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Concurrency, timing and confidence
# ---------------------------------------------------------------------------

from concurrent.futures import ThreadPoolExecutor as _ThreadPool  # noqa: E402
from concurrent.futures import as_completed as _as_completed  # noqa: E402

try:  # the cache module sits beside this one; keep working if it is absent
    from lifecycle_cache import (  # noqa: E402
        LifecycleCache as _LifecycleCache,
        SourceScheduler as _SourceScheduler,
        score as _score,
        RateLimiter as _RateLimiter,
        DEFAULT_TTL_DAYS as _DEFAULT_TTL_DAYS,
    )
except ImportError:  # pragma: no cover
    try:
        from .lifecycle_cache import (  # type: ignore
            LifecycleCache as _LifecycleCache,
            SourceScheduler as _SourceScheduler,
            score as _score,
            RateLimiter as _RateLimiter,
            DEFAULT_TTL_DAYS as _DEFAULT_TTL_DAYS,
        )
    except Exception:
        _LifecycleCache = None  # type: ignore
        _SourceScheduler = None  # type: ignore
        _score = None  # type: ignore
        _RateLimiter = None  # type: ignore
        _DEFAULT_TTL_DAYS = 45


def _default_cache_path(project_dir: str | None) -> str:
    """Where the lifecycle cache lives.

    Beside the project when there is one, so it travels with the design and a
    teammate's checkout starts warm; otherwise in the user cache directory.
    """
    if project_dir:
        return os.path.join(os.path.abspath(project_dir), "analysis",
                            "lifecycle_cache.json")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "kicad-happy", "lifecycle_cache.json")


_API_FNS = {
    "lcsc": query_lifecycle_lcsc,
    "digikey": query_lifecycle_digikey,
    "element14": query_lifecycle_element14,
    "mouser": query_lifecycle_mouser,
    "nexar": query_lifecycle_nexar,
}

# Queried unless the caller narrows the set. Nexar is absent on purpose: the
# evaluation licence it was built against allows a hundred part lookups for
# the life of the key, and this pipeline gets run dozens of times a day.
DEFAULT_SOURCES = ["lcsc", "digikey", "element14", "mouser"]

# Which sources can actually return a lifecycle status, as opposed to stock or
# fulfilment. The confidence ceiling is set from this, not from how many
# distributors were contacted.
STATUS_CAPABLE = {"digikey", "nexar"}


def _timed_query(fn, mpn: str, timeout: float, source: str = "",
                 limiter=None):
    """Run one distributor query, reporting how long it took and whether it worked.

    The distinction matters to the scheduler: a source that answers "no such
    part" in 300 ms is healthy and should keep its place near the front, while
    one that raises after its full deadline should be pushed back and given a
    longer rope next time. Both return no data, so elapsed time is the only
    thing that separates them.

    A third outcome is neither: the source refusing us because we asked too
    fast. That must not be recorded as slowness, because the cure is waiting
    longer between calls rather than waiting longer for an answer.
    """
    if limiter is not None and source:
        limiter.acquire(source)
    started = time.time()
    try:
        data = fn(mpn, timeout=timeout)
        return data, time.time() - started, True
    except Exception as exc:
        if limiter is not None and source and limiter.is_rejection(exc):
            limiter.penalise(source)
            return None, time.time() - started, True
        if isinstance(exc, (urllib.error.URLError, OSError, json.JSONDecodeError,
                            KeyError, ValueError, TypeError)):
            return None, time.time() - started, False
        raise


def audit_component(mpn: str, sources: list[str], project_dir: str | None = None,
                    delay: float = 1.0, cache=None, scheduler=None,
                    confidence_exit: float = 0.90, limiter=None) -> dict:
    """Query the available sources for one component's lifecycle + temperature.

    Cache first, then whatever is left, concurrently. ``delay`` is retained for
    callers that still pass it but is only honoured when running without a
    scheduler — the sleep it introduced was the audit's dominant cost, and the
    per-source deadline now does the rate-limiting job it was standing in for.
    """
    result = {"mpn": mpn, "sources": {}}
    best_status = "unknown"
    per_source_status: dict[str, str] = {}
    temp_data = None

    wanted = [s for s in _API_FNS
              if (s in sources if sources else s in DEFAULT_SOURCES)]

    # Try extraction cache first (no network, no delay)
    if project_dir:
        ext_temp = read_extraction_temperature(mpn, project_dir)
        if ext_temp:
            temp_data = ext_temp

    def absorb(source_name: str, data: dict | None) -> None:
        nonlocal best_status, temp_data
        if not data:
            return
        result["sources"][source_name] = data
        raw_status = data.get("status")
        if raw_status:
            normalized = _normalize_status(raw_status)
            per_source_status[source_name] = normalized
            if normalized != "unknown":
                best_status = normalized
        if not temp_data and data.get("temp_min_c") is not None:
            temp_data = {
                "temp_min_c": data["temp_min_c"],
                "temp_max_c": data["temp_max_c"],
                "source": f"api:{source_name}",
            }

    # Source zero. A cached answer costs nothing and is the reason a re-run
    # during a design session should not touch the network at all.
    remaining = list(wanted)
    cache_hits = 0
    if cache is not None:
        for source_name, data in cache.covered(mpn, wanted).items():
            absorb(source_name, data)
            cache_hits += 1
            if source_name in remaining:
                remaining.remove(source_name)

    if remaining and _score is not None:
        early = _score(per_source_status)
        if early["confidence"] >= confidence_exit:
            # Enough agreement already; the rest of the sources would only
            # confirm it, and confirmation is the expensive part.
            remaining = []

    if remaining:
        order = scheduler.order() if scheduler is not None else remaining
        ordered = [s for s in order if s in remaining]
        budgets = {s: (scheduler.budget(s) if scheduler is not None else 10.0)
                   for s in ordered}
        with _ThreadPool(max_workers=max(1, len(ordered))) as pool:
            futures = {}
            for source_name in ordered:
                fn = _API_FNS[source_name]
                futures[pool.submit(_timed_query, fn, mpn, budgets[source_name],
                                    source_name, limiter)] = source_name
            for fut in _as_completed(futures):
                source_name = futures[fut]
                try:
                    data, elapsed, ok = fut.result()
                except Exception:
                    data, elapsed, ok = None, budgets[source_name], False
                if cache is not None:
                    cache.observe(source_name, elapsed, ok)
                    if ok:
                        cache.put(mpn, source_name, data)
                absorb(source_name, data)

    result["status"] = best_status
    _non_active = {"obsolete", "discontinued", "last_time_buy", "nrnd"}
    has_active = any(s == "active" for s in per_source_status.values())
    has_non_active = any(s in _non_active for s in per_source_status.values())
    result["consensus_split"] = has_active and has_non_active
    result["per_source_status"] = per_source_status
    result["cache_hits"] = cache_hits
    if _score is not None:
        result["confidence"] = _score(per_source_status)
    if temp_data:
        result["temperature"] = temp_data
    return result


def find_alternatives(mpn: str,
                      sources: list[str] | None = None,
                      delay: float = 1.0) -> list[dict]:
    """Search for active alternative parts when a component is EOL/NRND/obsolete.

    Checks Mouser's SuggestedReplacement field first, then searches DigiKey
    and LCSC for parts with similar descriptions.

    Returns list of alternatives with mpn, manufacturer, source, status.
    """
    alternatives = []
    seen_mpns = {mpn.upper()}  # Don't suggest the original part

    # 1. Mouser SuggestedReplacement (already in query data — check sources)
    if not sources or "mouser" in sources:
        api_key = os.environ.get("MOUSER_SEARCH_API_KEY") or os.environ.get("MOUSER_PART_API_KEY")
        if api_key:
            try:
                time.sleep(delay)
                body = json.dumps({
                    "SearchByPartRequest": {
                        "mouserPartNumber": mpn,
                        "partSearchOptions": "",
                    }
                }).encode()
                url = f"https://api.mouser.com/api/v1/search/partnumber?apiKey={api_key}"
                req = urllib.request.Request(url, data=body,
                                            headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                for part in data.get("SearchResults", {}).get("Parts", []):
                    repl = part.get("SuggestedReplacement")
                    if repl and repl.upper() not in seen_mpns:
                        seen_mpns.add(repl.upper())
                        alternatives.append({
                            "mpn": repl,
                            "manufacturer": part.get("Manufacturer", ""),
                            "source": "mouser_suggestion",
                            "status": "suggested_replacement",
                        })
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass

    # 2. DigiKey keyword search for similar active parts
    if not sources or "digikey" in sources:
        auth = _get_digikey_token()
        if auth:
            token, client_id = auth
            # Search by the base part number (strip package suffix)
            base_mpn = re.sub(r'[A-Z]{0,3}$', '', mpn)  # Strip trailing package codes
            if len(base_mpn) >= 4:
                try:
                    time.sleep(delay)
                    body = json.dumps({"Keywords": base_mpn, "Limit": 5}).encode()
                    req = urllib.request.Request(
                        "https://api.digikey.com/products/v4/search/keyword",
                        data=body,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-DIGIKEY-Client-Id": client_id,
                            "Content-Type": "application/json",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        data = json.loads(resp.read())
                    for product in data.get("Products", []):
                        prod_mpn = product.get("ManufacturerProductNumber", "")
                        if not prod_mpn or prod_mpn.upper() in seen_mpns:
                            continue
                        # Only suggest active parts
                        prod_status = product.get("ProductStatus", {})
                        status_str = prod_status.get("Status", "") if isinstance(prod_status, dict) else str(prod_status)
                        if _normalize_status(status_str) == "active":
                            seen_mpns.add(prod_mpn.upper())
                            alternatives.append({
                                "mpn": prod_mpn,
                                "manufacturer": product.get("Manufacturer", {}).get("Name", ""),
                                "source": "digikey",
                                "status": "active",
                            })
                            if len(alternatives) >= 5:
                                break
                except (urllib.error.URLError, OSError, json.JSONDecodeError):
                    pass

    # 3. LCSC search for in-stock alternatives
    if not sources or "lcsc" in sources:
        base_mpn = re.sub(r'[A-Z]{0,3}$', '', mpn)
        if len(base_mpn) >= 4:
            try:
                time.sleep(delay)
                url = f"https://jlcsearch.tscircuit.com/api/search?q={urllib.parse.quote(base_mpn)}&limit=5&full=true"
                req = urllib.request.Request(url, headers={"User-Agent": "kicad-happy-lifecycle/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read())
                for comp in data.get("components", []):
                    extra = comp.get("extra", {})
                    comp_mpn = extra.get("mpn", "")
                    if not comp_mpn or comp_mpn.upper() in seen_mpns:
                        continue
                    stock = comp.get("stock", 0)
                    if stock > 0:
                        seen_mpns.add(comp_mpn.upper())
                        alternatives.append({
                            "mpn": comp_mpn,
                            "manufacturer": extra.get("manufacturer", ""),
                            "source": "lcsc",
                            "status": "in_stock",
                            "lcsc_stock": stock,
                        })
                        if len(alternatives) >= 5:
                            break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass

    return alternatives[:5]  # Cap at 5 suggestions


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def audit_bom(analysis_json: dict, project_dir: str | None = None,
              temp_range: tuple[float, float] | None = None,
              sources: list[str] | None = None,
              delay: float = 1.0,
              suggest_alternatives: bool = False,
              cache_path: str | None = None,
              ttl_days: float | None = None,
              concurrency: int = 8,
              confidence_exit: float = 0.90,
              report_threshold: float = 0.80,
              progress=None) -> dict:
    """Audit all components in the BOM for lifecycle and temperature.

    Parts are fetched concurrently and cached between runs. ``progress``, when
    given, is called with a dict after each part so a caller can drive a bar;
    the fraction it reports is of parts *settled* — resolved to at least
    ``report_threshold`` confidence — rather than merely attempted, because a
    part that came back unknown from every source has not been audited in any
    sense a reviewer would accept.
    """
    bom = analysis_json.get("bom", [])

    # Extract unique MPNs
    mpn_map = {}  # mpn -> list of references
    skipped = 0
    for entry in bom:
        if entry.get("dnp"):
            continue
        if entry.get("type", "") in _SKIP_TYPES:
            continue
        mpn = entry.get("mpn", "").strip()
        if not _is_real_mpn(mpn):
            skipped += 1
            continue
        mpn_map.setdefault(mpn, []).extend(entry.get("references", []))

    lifecycle_findings = []
    temperature_findings = []
    status_counts = {"active": 0, "nrnd": 0, "last_time_buy": 0,
                     "obsolete": 0, "discontinued": 0, "unknown": 0}
    grade_counts = {}
    temp_ok = 0
    temp_fail = 0

    total = len(mpn_map)

    # Cache and scheduler are shared across the whole run: one read at the
    # start, one write at the end, and the timing each part observes informs
    # the sources the next part asks first.
    cache = None
    scheduler = None
    if _LifecycleCache is not None:
        path = cache_path or _default_cache_path(project_dir)
        cache = _LifecycleCache(path, ttl_days if ttl_days is not None else _DEFAULT_TTL_DAYS)
        scheduler = _SourceScheduler(cache, [s for s in _API_FNS
                                             if (s in sources if sources
                                                 else s in DEFAULT_SOURCES)])
    limiter = _RateLimiter(cache) if _RateLimiter is not None else None

    ordered_mpns = sorted(mpn_map.items())
    settled = 0
    results: dict[str, dict] = {}
    if cache is not None and scheduler is not None:
        cached_fully = sum(
            1 for mpn, _ in ordered_mpns
            if len(cache.covered(mpn, scheduler.sources, count=False)) == len(scheduler.sources)
        )
        est = scheduler.estimate(total, concurrency, cached_fully)
        print("lifecycle: %d parts, %d fully cached, estimate %.0fs at %d workers (%s)"
              % (total, cached_fully, est["seconds"], concurrency, est["basis"]),
              file=sys.stderr)

    def _one(item):
        mpn, refs = item
        return mpn, audit_component(mpn, sources or [], project_dir, delay,
                                    cache=cache, scheduler=scheduler,
                                    confidence_exit=confidence_exit,
                                    limiter=limiter)

    with _ThreadPool(max_workers=max(1, concurrency)) as pool:
        for done, (mpn, data) in enumerate(pool.map(_one, ordered_mpns), start=1):
            results[mpn] = data
            conf = (data.get("confidence") or {}).get("confidence", 0.0)
            if conf >= report_threshold:
                settled += 1
            if progress is not None:
                progress({"done": done, "total": total, "settled": settled,
                          "fraction_settled": settled / total if total else 1.0,
                          "mpn": mpn, "confidence": conf})
            print("[%d/%d] %s  conf=%.2f" % (done, total, mpn, conf), file=sys.stderr)

    if cache is not None:
        try:
            cache.save()
        except OSError as exc:
            print("lifecycle: cache not written (%s)" % exc, file=sys.stderr)
        print("lifecycle: cache %s; %d/%d parts settled at >=%.0f%% confidence"
              % (cache.stats, settled, total, report_threshold * 100), file=sys.stderr)

    for i, (mpn, refs) in enumerate(ordered_mpns):
        data = results[mpn]

        # Lifecycle
        status = data.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        finding = {
            "mpn": mpn,
            "references": sorted(refs),
            "status": status,
            "sources": data.get("sources", {}),
        }

        # Flag non-active statuses
        if status in ("nrnd", "last_time_buy", "obsolete", "discontinued"):
            alert_map = {
                "nrnd": "NRND — not recommended for new designs, consider replacement",
                "last_time_buy": "Last Time Buy — order soon or find alternative",
                "obsolete": "Obsolete — find replacement part",
                "discontinued": "Discontinued — find replacement part",
            }
            finding["alert"] = alert_map.get(status, "")
            # Check for suggested replacement from API data
            for src_data in data.get("sources", {}).values():
                repl = src_data.get("suggested_replacement")
                if repl:
                    finding["suggested_replacement"] = repl
                    break
            # Search for alternatives if requested
            if suggest_alternatives:
                print(f"  Searching for alternatives...", file=sys.stderr)
                alts = find_alternatives(mpn, sources, delay)
                if alts:
                    finding["alternatives"] = alts

        rule_info = _LIFECYCLE_STATUS_RULES.get(status)
        if rule_info:
            rule_id, severity = rule_info
            consensus_split = data.get('consensus_split', False)
            per_source = data.get('per_source_status', {})
            # Demote ERROR → WARNING when distributors disagree. The part is
            # still orderable from at least one active source, so "obsolete"
            # overstates the supply risk.
            split_note = ''
            if consensus_split and severity == 'error':
                severity = 'warning'
                active_srcs = sorted(s for s, st in per_source.items() if st == 'active')
                eol_srcs = sorted(s for s, st in per_source.items() if st != 'active')
                split_note = (f" Distributor disagreement: {', '.join(active_srcs)} "
                              f"list it as active while {', '.join(eol_srcs)} "
                              f"flag EOL. Part is orderable today.")
            finding['detector'] = 'audit_bom'
            finding['rule_id'] = rule_id
            finding['category'] = 'lifecycle'
            finding['severity'] = severity
            finding['confidence'] = 'deterministic'
            finding['evidence_source'] = 'api_lookup'
            finding['summary'] = f"{mpn}: {status} ({len(refs)} ref(s))"
            if per_source:
                finding['per_source_status'] = per_source
            if consensus_split:
                finding['consensus_split'] = True
            finding['description'] = (finding.get('alert', f'Component {mpn} is {status}.')
                                      + split_note)
            finding['components'] = sorted(refs)
            finding['nets'] = []
            finding['pins'] = []
            finding['recommendation'] = (
                f"Replace {mpn} — part is {status}." if not consensus_split
                else f"Verify current availability before committing to {mpn}; "
                     f"consider locking to the active distributor or finding an "
                     f"alternate.")
            finding['report_context'] = {'section': 'Lifecycle', 'impact': 'Supply chain risk', 'standard_ref': ''}
        else:
            finding['detector'] = 'audit_bom'
            finding['rule_id'] = 'LC-ACT'
            finding['category'] = 'lifecycle'
            finding['severity'] = 'info'
            finding['summary'] = f"{mpn}: active ({len(refs)} ref(s))"
            finding['components'] = sorted(refs)
            finding['nets'] = []
            finding['pins'] = []
            finding['report_context'] = {'section': 'Lifecycle', 'impact': '', 'standard_ref': ''}

        lifecycle_findings.append(finding)

        # LC-005: Single-source detection
        if status == 'active':
            active_sources = [src_name for src_name, src_data in finding.get('sources', {}).items()
                              if src_data.get('status') in ('active', 'Active', None)
                              and src_data.get('found', True)]
            total_queried = len(finding.get('sources', {}))
            if total_queried >= 2 and len(active_sources) == 1:
                lifecycle_findings.append({
                    'mpn': mpn,
                    'references': sorted(refs),
                    'status': 'active',
                    'single_source': True,
                    'source_name': active_sources[0],
                    'detector': 'audit_bom',
                    'rule_id': 'LC-005',
                    'category': 'lifecycle',
                    'severity': 'info',
                    'confidence': 'deterministic',
                    'evidence_source': 'datasheet',
                    'summary': f'{mpn}: single source ({active_sources[0]})',
                    'description': f'Component {mpn} ({len(refs)} ref(s)) is only available from {active_sources[0]} out of {total_queried} sources checked.',
                    'components': sorted(refs),
                    'nets': [],
                    'pins': [],
                    'recommendation': 'Consider qualifying an alternative source for supply chain resilience.',
                    'report_context': {'section': 'Lifecycle', 'impact': 'Supply chain fragility', 'standard_ref': ''},
                })

        # LC-006: Long lead time
        max_lead_weeks = 0
        lead_source = ''
        for src_name, src_data in finding.get('sources', {}).items():
            lt = src_data.get('lead_time')
            if lt:
                weeks = 0
                if isinstance(lt, (int, float)):
                    weeks = int(lt)
                elif isinstance(lt, str):
                    m = re.search(r'(\d+)', lt)
                    if m:
                        weeks = int(m.group(1))
                        if 'day' in lt.lower():
                            weeks = weeks // 7
                if weeks > max_lead_weeks:
                    max_lead_weeks = weeks
                    lead_source = src_name

        if max_lead_weeks > 12:
            severity = 'warning' if max_lead_weeks > 26 else 'info'
            lifecycle_findings.append({
                'mpn': mpn,
                'references': sorted(refs),
                'lead_weeks': max_lead_weeks,
                'lead_source': lead_source,
                'detector': 'audit_bom',
                'rule_id': 'LC-006',
                'category': 'lifecycle',
                'severity': severity,
                'confidence': 'deterministic',
                'evidence_source': 'datasheet',
                'summary': f'{mpn}: {max_lead_weeks} week lead time',
                'description': f'Component {mpn} has {max_lead_weeks} week lead time (from {lead_source}).',
                'components': sorted(refs),
                'nets': [],
                'pins': [],
                'recommendation': f'Pre-order or stock {mpn} — long lead time risk.',
                'report_context': {'section': 'Lifecycle', 'impact': 'Procurement delay risk', 'standard_ref': ''},
            })

        # Temperature
        temp = data.get("temperature")
        if temp and temp_range:
            design_min, design_max = temp_range
            comp_min = temp["temp_min_c"]
            comp_max = temp["temp_max_c"]
            comp_grade = _classify_temp_grade(comp_min, comp_max)
            grade_counts[comp_grade] = grade_counts.get(comp_grade, 0) + 1

            below_min = comp_min > design_min
            above_max = comp_max < design_max

            if below_min or above_max:
                temp_fail += 1
                temperature_findings.append({
                    "mpn": mpn,
                    "references": sorted(refs),
                    "component_range": {"min_c": comp_min, "max_c": comp_max},
                    "component_grade": comp_grade,
                    "design_range": {"min_c": design_min, "max_c": design_max},
                    "data_source": temp.get("source", "unknown"),
                    "severity": "warning",
                    "alert": (f"{comp_grade.capitalize()} ({comp_min} to {comp_max}°C) component "
                              f"in {_classify_temp_grade(design_min, design_max)} "
                              f"({design_min} to {design_max}°C) design"),
                    "violations": {
                        "below_min": below_min,
                        "above_max": above_max,
                        "min_shortfall_c": design_min - comp_min if below_min else 0,
                        "max_shortfall_c": design_max - comp_max if above_max else 0,
                    },
                    "detector": "audit_bom",
                    "rule_id": "LT-001",
                    "category": "temperature",
                    "confidence": "deterministic",
                    "evidence_source": "api_lookup" if temp.get("source") else "heuristic_rule",
                    "summary": f"{mpn}: rated {comp_grade} ({comp_min}C to {comp_max}C), design needs {design_min}C to {design_max}C",
                    "components": sorted(refs),
                    "nets": [],
                    "pins": [],
                    "recommendation": f"Select component rated for full {design_min}C to {design_max}C range.",
                    "report_context": {"section": "Temperature", "impact": "Operating range violation", "standard_ref": ""},
                })
            else:
                temp_ok += 1
        elif temp:
            comp_grade = _classify_temp_grade(temp["temp_min_c"], temp["temp_max_c"])
            grade_counts[comp_grade] = grade_counts.get(comp_grade, 0) + 1

    # Build output
    result = {
        "analyzer_type": "lifecycle",
        "schema_version": "1.4.0",
        "audit_date": datetime.now().astimezone().isoformat(timespec='seconds'),
        "components_checked": total,
        "components_with_mpn": total,
        "components_without_mpn": skipped,
        "sources_available": list({src for f in lifecycle_findings for src in f.get("sources", {})}),
        "findings": lifecycle_findings,
        "lifecycle_summary": status_counts,
    }

    observations = []
    for status_key in ("nrnd", "last_time_buy", "obsolete", "discontinued"):
        count = status_counts.get(status_key, 0)
        if count:
            labels = {
                "nrnd": "Not Recommended for New Designs",
                "last_time_buy": "Last Time Buy",
                "obsolete": "Obsolete",
                "discontinued": "Discontinued",
            }
            observations.append(f"{count} component(s) {labels[status_key]}")

    if temp_range:
        design_min, design_max = temp_range
        result["findings"] = result["findings"] + temperature_findings
        result["temperature_summary"] = {
            "design_target": {
                "min_c": design_min,
                "max_c": design_max,
                "grade": _classify_temp_grade(design_min, design_max),
            },
            "components_checked": temp_ok + temp_fail,
            "components_meeting_spec": temp_ok,
            "components_failing_spec": temp_fail,
            "grade_distribution": grade_counts,
        }
        if temp_fail:
            observations.append(
                f"{temp_fail} component(s) don't meet {_classify_temp_grade(design_min, design_max)} "
                f"temperature range ({design_min} to {design_max}°C)"
            )

    if observations:
        result["observations"] = observations

    all_findings = result.get("findings", [])
    sev_counts = {"error": 0, "warning": 0, "info": 0}
    for f in all_findings:
        s = (f.get("severity") or "info").lower()
        if s in ("critical", "high", "error"):
            sev_counts["error"] += 1
        elif s in ("medium", "low", "warning"):
            sev_counts["warning"] += 1
        else:
            sev_counts["info"] += 1
    result["summary"] = {
        "total_findings": len(all_findings),
        "by_severity": sev_counts,
        "components_checked": total,
        "lifecycle_issues": len(lifecycle_findings),
        "temperature_issues": len(temperature_findings),
    }
    from finding_schema import compute_trust_summary
    result["trust_summary"] = compute_trust_summary(all_findings)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Component lifecycle and temperature audit",
    )
    parser.add_argument(
        "input",
        help="Path to analyzer JSON output",
    )
    parser.add_argument(
        "--temp-range",
        help="Design temperature range: preset name (commercial, industrial, "
             "extended, automotive, military) or 'min,max' in °C. When the min "
             "is negative, use the = form so argparse doesn't read it as a flag: "
             "--temp-range=\"-40,85\"",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: stdout)",
    )
    parser.add_argument(
        "--analysis-dir",
        help="Write lifecycle.json to this directory (analysis folder convention). "
             "Routes the output through the current run via the manifest, matching "
             "every other analyzer. Ignored if --output is also passed.",
    )
    parser.add_argument(
        "--only",
        help="Query only specific sources (comma-separated: digikey,mouser,lcsc,element14)",
    )
    parser.add_argument(
        "--nexar", action="store_true",
        help="Also query Nexar. OFF by default: an evaluation licence caps "
             "part lookups for the life of the key, so this must be asked for",
    )
    parser.add_argument(
        "--cache", dest="cache_path", default=None,
        help="Lifecycle cache file (default: <project>/analysis/lifecycle_cache.json)",
    )
    parser.add_argument(
        "--ttl-days", type=float, default=None,
        help="How long a cached distributor answer stays fresh (default: %d)" % _DEFAULT_TTL_DAYS,
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore and do not write the lifecycle cache",
    )
    parser.add_argument(
        "--concurrency", type=int, default=8,
        help="Parts fetched at once (default: 8)",
    )
    parser.add_argument(
        "--confidence-exit", type=float, default=0.90,
        help="Stop querying a part once confidence reaches this (default: 0.90)",
    )
    parser.add_argument(
        "--report-threshold", type=float, default=0.80,
        help="Confidence at which a part counts as settled (default: 0.80)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Seconds between API calls (default: 1.0)",
    )
    parser.add_argument(
        "--suggest-alternatives", action="store_true",
        help="Search for replacement parts when EOL/NRND/obsolete (extra API calls)",
    )
    parser.add_argument(
        "--only-deterministic", action="store_true",
        help="Read raw analysis/<run>/<analyzer>.json instead of "
             "analysis/merged/<run>/<analyzer>.json. "
             "Strips Layer 2 overlays for CI/offline use (Phase 4 spec §3.4).",
    )
    args = parser.parse_args()

    # Load analyzer JSON (honor --only-deterministic: skip merged/ overlay)
    input_path = Path(args.input)
    if not args.only_deterministic:
        candidate = input_path.parent.parent / "merged" / input_path.parent.name / input_path.name
        if candidate.exists():
            input_path = candidate
    with open(input_path) as f:
        analysis = json.load(f)

    # Resolve project directory from analyzer JSON
    source_file = analysis.get("file", "")
    project_dir = str(Path(source_file).parent) if source_file else str(input_path.parent)

    # Parse temperature range
    temp_range = None
    if args.temp_range:
        if args.temp_range in _TEMP_PRESETS:
            temp_range = _TEMP_PRESETS[args.temp_range]
        else:
            parts = args.temp_range.split(",")
            if len(parts) == 2:
                try:
                    temp_range = (float(parts[0]), float(parts[1]))
                except ValueError:
                    print(f"Error: Invalid temp range '{args.temp_range}'. "
                          f"Use preset name or 'min,max'.", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Error: Invalid temp range '{args.temp_range}'. "
                      f"Presets: {', '.join(_TEMP_PRESETS.keys())}", file=sys.stderr)
                sys.exit(1)

    # Parse sources
    sources = args.only.split(",") if args.only else []

    # Run audit
    result = audit_bom(analysis, project_dir=project_dir, temp_range=temp_range,
                       sources=(sources or (DEFAULT_SOURCES + ["nexar"]
                                            if getattr(args, "nexar", False)
                                            else DEFAULT_SOURCES)),
                       delay=args.delay,
                       cache_path=(None if getattr(args, "no_cache", False) else args.cache_path),
                       ttl_days=(0.0 if getattr(args, "no_cache", False) else args.ttl_days),
                       concurrency=args.concurrency,
                       confidence_exit=args.confidence_exit,
                       report_threshold=args.report_threshold,
                       suggest_alternatives=args.suggest_alternatives)

    # Output
    output_json = json.dumps(result, indent=2)
    output_path = args.output
    analysis_dir_mode = (not output_path
                         and hasattr(args, "analysis_dir")
                         and args.analysis_dir)

    if analysis_dir_mode:
        import tempfile
        from analysis_cache import overwrite_current, CANONICAL_OUTPUTS, get_current_run
        analysis_dir = args.analysis_dir
        if not os.path.isabs(analysis_dir):
            analysis_dir = os.path.abspath(analysis_dir)
        filename = CANONICAL_OUTPUTS.get("lifecycle", "lifecycle.json")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_out = os.path.join(tmp_dir, filename)
            with open(tmp_out, "w") as f:
                f.write(output_json)
            overwrite_current(analysis_dir, tmp_dir, source_hashes=None)
        current = get_current_run(analysis_dir)
        if current:
            out_path = os.path.join(current[0], filename)
        else:
            out_path = os.path.join(analysis_dir, filename)
        print(f"Audit written to {out_path}", file=sys.stderr)
    elif output_path:
        with open(output_path, "w") as f:
            f.write(output_json)
        print(f"Audit written to {output_path}", file=sys.stderr)
    else:
        print(output_json)

    # Summary to stderr
    summary = result.get("lifecycle_summary", {})
    total = result.get("components_checked", 0)
    print(f"\nLifecycle audit: {total} components checked", file=sys.stderr)
    for status_key in ("active", "nrnd", "last_time_buy", "obsolete", "discontinued", "unknown"):
        count = summary.get(status_key, 0)
        if count:
            print(f"  {status_key}: {count}", file=sys.stderr)

    if result.get("temperature_summary"):
        ts = result["temperature_summary"]
        print(f"Temperature audit: {ts['components_checked']} checked, "
              f"{ts['components_failing_spec']} failing "
              f"({ts['design_target']['grade']} range)", file=sys.stderr)

    for obs in result.get("observations", []):
        print(f"  ! {obs}", file=sys.stderr)


if __name__ == "__main__":
    main()
