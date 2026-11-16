"""ERCOT Public API client (https://api.ercot.com/api/public-reports) and report parsers.

Authentication needs both (ERCOT API Explorer docs; gridstatus 0.37 ercot_api.py):
  * an ID token from ERCOT's Azure B2C "ROPC" flow (username + password), valid
    about one hour, sent as `Authorization: Bearer <id_token>`;
  * the account's subscription key, sent as `Ocp-Apim-Subscription-Key`. We use
    the PRIMARY key only.

The documented limit is 30 requests a minute; we pace at one request per 2.5 s.

ERCOT reportedly blocks non-US IPs, and CLAUDE.md rule 8 says every ERCOT call
runs from Railway (US region). `ErcotPublic` refuses to start unless the process
runs in a deployed Railway replica in a US region (`require_us_railway`). Note
that `railway run` executes on the laptop, so it does not count.

Credentials are read from the environment and never printed, logged or put in
an error message. `ErcotCredentials` hides them from repr.

Archives: every report ERCOT posts is kept as a document with a `docId` and a
`postDatetime` (Central prevailing time). Listing is
`GET /archive/{emil}?postDatetimeFrom&postDatetimeTo&size&page`; download is
`POST /archive/{emil}/download {"docIds": [...]}`, which returns a zip holding
one `<docId>.zip` per document, each holding one CSV. Keeping every posted
version is what lets us find the FIRST complete report of an operating day,
which is what Kalshi settles on.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import random
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
    "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
)
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"  # public client id of ERCOT's API app
BASE_URL = "https://api.ercot.com/api/public-reports"
TOKEN_LIFETIME_S = 3600
TOKEN_REFRESH_MARGIN_S = 600  # refresh 10 minutes early
MIN_REQUEST_INTERVAL_S = 2.5  # 24 a minute, under the documented 30
ARCHIVE_PAGE_SIZE = 1000  # documented maximum for archive listings
DOWNLOAD_BATCH = 100  # docIds per bulk download (keeps each response small)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
CENTRAL = ZoneInfo("America/Chicago")

ENV_USERNAME = "ERCOT_API_USERNAME"
ENV_PASSWORD = "ERCOT_API_PASSWORD"  # the variable NAME, not a secret
ENV_SUBSCRIPTION_KEY = "ERCOT_PUBLIC_API_SUBSCRIPTION_KEY"

# EMIL ids of the products we use.
ACTUAL_LOAD_BY_FORECAST_ZONE = "np6-346-cd"  # Kalshi KXTXERCOTPEAKD settlement source
LOAD_FORECAST_BY_WEATHER_ZONE = "np3-565-cd"  # seven-day load forecast by model


class ErcotError(RuntimeError):
    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"ERCOT {status} on {path}: {body[:300]}")
        self.status = status
        self.path = path


class ErcotHostError(RuntimeError):
    """Raised when ERCOT would be called from somewhere other than Railway US."""


def require_us_railway(env: dict[str, str] | None = None) -> str:
    """Return the Railway replica region, or raise if this is not a US Railway replica.

    RAILWAY_REPLICA_REGION is set inside deployed replicas only (e.g.
    "us-east4-eqdc4a"); `railway run` on a laptop does not provide it.
    """
    env = dict(os.environ) if env is None else env
    region = env.get("RAILWAY_REPLICA_REGION", "")
    if not region.startswith("us-"):
        raise ErcotHostError(
            "ERCOT calls run only from a deployed Railway service in a US region "
            f"(RAILWAY_REPLICA_REGION={region or 'unset'}). See CLAUDE.md rule 8."
        )
    return region


@dataclass(frozen=True)
class ErcotCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)
    subscription_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ErcotCredentials:
        env = dict(os.environ) if env is None else env
        names = (ENV_USERNAME, ENV_PASSWORD, ENV_SUBSCRIPTION_KEY)
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise ErcotHostError(f"missing ERCOT credentials: {', '.join(missing)}")
        return cls(env[ENV_USERNAME], env[ENV_PASSWORD], env[ENV_SUBSCRIPTION_KEY])


# ---------------------------------------------------------------------------
# Archive listing
# ---------------------------------------------------------------------------


def parse_post_datetime(text: str) -> datetime:
    """ERCOT postDatetime ("2026-11-15T05:31:07", Central prevailing time) to UTC.

    In the repeated fall-back hour the local time is ambiguous; we take the first
    occurrence (fold=0). Reports we use are not posted between 01:00 and 02:00.
    """
    raw = text.strip().replace("Z", "")
    local = datetime.fromisoformat(raw[:19])
    return local.replace(tzinfo=CENTRAL).astimezone(ZoneInfo("UTC"))


@dataclass(frozen=True)
class ArchiveDoc:
    emil: str
    doc_id: int
    post_datetime_utc: datetime
    post_datetime_raw: str  # as ERCOT gave it, Central prevailing time
    friendly_name: str


def parse_archive_page(emil: str, payload: dict[str, Any]) -> tuple[list[ArchiveDoc], int]:
    """One page of `GET /archive/{emil}`: (documents, total pages)."""
    meta = payload.get("_meta") or {}
    docs = []
    for a in payload.get("archives") or []:
        doc_id = a.get("docId")
        if doc_id is None:  # fall back to the download link "...?download=<docId>"
            href = ((a.get("_links") or {}).get("endpoint") or {}).get("href", "")
            doc_id = href.rsplit("=", 1)[-1]
        posted = a.get("postDatetime") or ""
        docs.append(
            ArchiveDoc(
                emil=emil,
                doc_id=int(doc_id),
                post_datetime_utc=parse_post_datetime(posted),
                post_datetime_raw=posted,
                friendly_name=a.get("friendlyName") or "",
            )
        )
    return docs, int(meta.get("totalPages") or 0)


def unpack_bulk_download(data: bytes) -> dict[int, bytes]:
    """Bulk-download zip -> {docId: inner zip bytes}. Inner files are named <docId>.zip."""
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as outer:
        for name in outer.namelist():
            stem = name.rsplit("/", 1)[-1].split(".", 1)[0]
            out[int(stem)] = outer.read(name)
    return out


def read_doc_csv(doc_zip: bytes) -> tuple[str, str]:
    """A document's zip -> (csv file name, csv text). Exactly one CSV is expected."""
    with zipfile.ZipFile(io.BytesIO(doc_zip)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in document, found {z.namelist()}")
        return names[0], z.read(names[0]).decode("utf-8-sig")


# ---------------------------------------------------------------------------
# NP6-346-CD: Actual System Load by Forecast Zone
# ---------------------------------------------------------------------------

FZN_ZONES = ("NORTH", "SOUTH", "WEST", "HOUSTON")
FZN_COLUMNS = ("OperDay", "HourEnding", *FZN_ZONES, "TOTAL", "DSTFlag")


@dataclass(frozen=True)
class LoadHour:
    oper_day: date
    hour_ending: int  # 1..24, Central prevailing time
    repeated: bool  # True for the second 01:00-02:00 hour on the fall-back day
    zones: dict[str, Decimal | None]
    total: Decimal | None  # exactly as published; None if blank


def _decimal(text: str) -> Decimal | None:
    t = (text or "").strip().replace(",", "")
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {text!r}") from exc


def _us_date(text: str) -> date:
    """'11/14/2026' -> date(2026, 11, 14)."""
    month, day, year = (int(x) for x in text.strip().split("/"))
    return date(year, month, day)


def _hour_ending(text: str) -> int:
    t = text.strip()
    h = int(t.split(":", 1)[0]) if ":" in t else int(t)
    if not 1 <= h <= 24:
        raise ValueError(f"hour ending out of range: {text!r}")
    return h


def _flag(text: str) -> bool:
    t = (text or "").strip().upper()
    if t in ("Y", "TRUE"):
        return True
    if t in ("N", "FALSE", ""):
        return False
    raise ValueError(f"unknown DSTFlag {text!r}")


def parse_load_by_forecast_zone_csv(text: str) -> list[LoadHour]:
    """Parse an NP6-346-CD CSV. Fails loudly on any header it does not expect."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader)]
    if tuple(header) != FZN_COLUMNS:
        raise ValueError(f"unexpected NP6-346-CD header {header}; expected {list(FZN_COLUMNS)}")
    rows = []
    for rec in reader:
        if not rec or not any(c.strip() for c in rec):
            continue
        r = dict(zip(header, rec, strict=True))
        rows.append(
            LoadHour(
                oper_day=_us_date(r["OperDay"]),
                hour_ending=_hour_ending(r["HourEnding"]),
                repeated=_flag(r["DSTFlag"]),
                zones={z: _decimal(r[z]) for z in FZN_ZONES},
                total=_decimal(r["TOTAL"]),
            )
        )
    return rows


def expected_hours(day: date) -> int:
    """Hours in an operating day in Central prevailing time: 23, 24 or 25."""
    start = datetime(day.year, day.month, day.day, tzinfo=CENTRAL)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=CENTRAL)
    return round(
        (end.astimezone(ZoneInfo("UTC")) - start.astimezone(ZoneInfo("UTC"))).total_seconds() / 3600
    )


def is_complete(rows: list[LoadHour], day: date) -> bool:
    """Every hour of the operating day present once, each with a TOTAL."""
    mine = [r for r in rows if r.oper_day == day]
    keys = {(r.hour_ending, r.repeated) for r in mine}
    return (
        len(mine) == expected_hours(day)
        and len(keys) == len(mine)
        and all(r.total is not None for r in mine)
    )


@dataclass(frozen=True)
class DayVersion:
    doc: ArchiveDoc
    rows: list[LoadHour]


def first_complete(versions: Iterable[DayVersion], day: date) -> DayVersion | None:
    """The earliest-posted version that holds a complete operating day `day`."""
    for v in sorted(versions, key=lambda v: (v.doc.post_datetime_utc, v.doc.doc_id)):
        if is_complete(v.rows, day):
            return v
    return None


def daily_peak(rows: list[LoadHour], day: date) -> Decimal:
    """Highest TOTAL for the operating day, as published (no rounding)."""
    totals = [r.total for r in rows if r.oper_day == day and r.total is not None]
    if not totals:
        raise ValueError(f"no TOTAL values for {day}")
    return max(totals)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class ErcotPublic:
    """Paced, retrying client for the ERCOT Public API. Construct on Railway only."""

    def __init__(
        self,
        creds: ErcotCredentials,
        *,
        min_request_interval_s: float = MIN_REQUEST_INTERVAL_S,
        timeout_s: float = 60.0,
        max_attempts: int = 6,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if transport is None:  # tests inject a transport; real use must be on Railway US
            require_us_railway()
        self._creds = creds
        self._min_interval = min_request_interval_s
        self._max_attempts = max_attempts
        self._last_request = 0.0
        self._token: str | None = None
        self._token_expires = 0.0
        self.request_count = 0
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": "pmw-research/0.1"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _pace(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.05))
        self._last_request = time.monotonic()

    def _fetch_token(self) -> None:
        self._pace()
        self.request_count += 1
        resp = self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "password",
                "username": self._creds.username,
                "password": self._creds.password,
                "response_type": "id_token",
                "scope": f"openid {CLIENT_ID} offline_access",
                "client_id": CLIENT_ID,
            },
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("id_token") if isinstance(body, dict) else None
        if resp.status_code != 200 or not token:
            # B2C error bodies carry an error code and description, never the password.
            code = body.get("error", "") if isinstance(body, dict) else ""
            raise ErcotError(resp.status_code, "token", f"token request failed {code}".strip())
        self._token = token
        self._token_expires = time.monotonic() + TOKEN_LIFETIME_S - TOKEN_REFRESH_MARGIN_S

    def _headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_expires:
            self._fetch_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Ocp-Apim-Subscription-Key": self._creds.subscription_key,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        delay = 2.0
        refreshed = False
        for attempt in range(1, self._max_attempts + 1):
            headers = self._headers()
            self._pace()
            self.request_count += 1
            try:
                resp = self._client.request(
                    method, BASE_URL + path, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                err: Exception = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 401 and not refreshed:
                    self._token = None  # expired early: one fresh token, then retry
                    refreshed = True
                    continue
                err = ErcotError(resp.status_code, path, resp.text)
                if resp.status_code not in RETRYABLE_STATUS:
                    raise err
            if attempt == self._max_attempts:
                raise err
            log.warning("ercot retry #%d on %s after %s", attempt, path, err)
            time.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")

    # ---- archives ----

    def archive_page(
        self,
        emil: str,
        post_from: datetime,
        post_to: datetime,
        *,
        page: int = 1,
        size: int = ARCHIVE_PAGE_SIZE,
    ) -> tuple[list[ArchiveDoc], int, int]:
        """One page of documents posted in [post_from, post_to]: (docs, pages, records)."""
        if post_from.tzinfo is None or post_to.tzinfo is None:
            raise ValueError("archive needs timezone-aware datetimes")
        resp = self._request(
            "GET",
            f"/archive/{emil}",
            params={
                # ERCOT filters on Central prevailing local time.
                "postDatetimeFrom": post_from.astimezone(CENTRAL).strftime("%Y-%m-%dT%H:%M:%S"),
                "postDatetimeTo": post_to.astimezone(CENTRAL).strftime("%Y-%m-%dT%H:%M:%S"),
                "size": size,
                "page": page,
            },
        )
        payload = resp.json()
        docs, pages = parse_archive_page(emil, payload)
        records = int((payload.get("_meta") or {}).get("totalRecords") or len(docs))
        return docs, pages, records

    def archive(self, emil: str, post_from: datetime, post_to: datetime) -> list[ArchiveDoc]:
        """Every document of `emil` posted in [post_from, post_to] (timezone-aware)."""
        docs: list[ArchiveDoc] = []
        page, pages = 1, 1
        while page <= pages:
            got, pages, _ = self.archive_page(emil, post_from, post_to, page=page)
            docs.extend(got)
            page += 1
        return docs

    def download(self, emil: str, doc_ids: list[int]) -> dict[int, bytes]:
        """{docId: document zip bytes}. Raises if any requested document is missing."""
        out: dict[int, bytes] = {}
        for i in range(0, len(doc_ids), DOWNLOAD_BATCH):
            batch = doc_ids[i : i + DOWNLOAD_BATCH]
            resp = self._request("POST", f"/archive/{emil}/download", json={"docIds": batch})
            got = unpack_bulk_download(resp.content)
            missing = set(batch) - set(got)
            if missing:
                raise ErcotError(
                    200, f"/archive/{emil}/download", f"missing docIds {sorted(missing)}"
                )
            out.update(got)
        return out
