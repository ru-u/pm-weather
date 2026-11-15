"""NBM station text bulletins: archive access and the NBP (probabilistic) parser.

Archive (public, per hourly cycle, back to at least 2021):
    https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.YYYYMMDD/HH/text/blend_nbptx.tHHz

Each file is 25 to 35 MB of fixed-width text, one block per station, and a
station sits at the same byte offset in every cycle until the station list or
the NBM version changes. So a station's block is fetched with one small HTTP
Range request, and the whole file is downloaded only to (re)learn the offsets.

NBP elements used here (NBM text card, MDL):
    S24P1/2/5/7/9  10/25/50/75/90th percentile 24-hour snowfall, 1/10 inch
    Q24P1/2/5/7/9  same for 24-hour liquid precipitation, 1/100 inch
    I24P1/2/5/7/9  same for 24-hour ice accumulation, 1/100 inch
"For the X percentile there is an X% probability that the element will be equal
to or below the value listed." The 24-hour periods end at 12Z.

Issue time is not availability time. The S3 `Last-Modified` of each file is
kept with every block; see `available_at`.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BUCKET_URL = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
PRODUCTS = {"nbp": "blend_nbptx", "nbs": "blend_nbstx"}
RANGE_BYTES = 4500  # larger than any CONUS station block seen (2997 to 3807 bytes)
_HEADER_RE = re.compile(
    r"^ ?(?P<station>[A-Z0-9]{3,8}) +NBM (?P<version>V[\d.]+) (?P<product>NB[A-Z]) GUIDANCE +"
    r"(?P<mon>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{4}) +(?P<hhmm>\d{4}) UTC"
)
_ANY_HEADER_RE = re.compile(rb"(?m)^ ?([A-Z0-9]{3,8}) +NBM V[\d.]+ NB[A-Z] GUIDANCE")
PERCENTILES = {"P1": 10, "P2": 25, "P5": 50, "P7": 75, "P9": 90}
LABEL_WIDTH = 6  # one space plus a five-character element label, e.g. " S24P1"
# Normal lag from cycle time to public availability, and the cut-off beyond
# which an S3 timestamp is treated as a late archive upload, not a real delay.
NORMAL_LAG = timedelta(hours=2)
MAX_TRUSTED_LAG = timedelta(hours=3)


def bulletin_url(product: str, cycle: datetime) -> str:
    return f"{BUCKET_URL}/blend.{cycle:%Y%m%d}/{cycle:%H}/text/{PRODUCTS[product]}.t{cycle:%H}z"


def available_at(cycle: datetime, last_modified: datetime | None) -> datetime:
    """When a cycle can be assumed to have been public.

    The S3 timestamp when it is within 3 hours of the cycle (the archive is
    normally written 35 to 100 minutes after the cycle). Otherwise the file was
    uploaded late in a batch and the timestamp says nothing about real-time
    availability, so use cycle + 2 hours, which is later than any normal lag seen.
    """
    if last_modified is not None and timedelta(0) <= last_modified - cycle <= MAX_TRUSTED_LAG:
        return last_modified
    return cycle + NORMAL_LAG


# ---------- parsing ----------


@dataclass(frozen=True)
class Period24:
    """One 24-hour period of an NBP block, ending at `valid_end` (12Z)."""

    fhr: int
    valid_end: datetime
    snow_in: dict[int, float | None]  # percentile -> inches
    qpf_in: dict[int, float | None]
    ice_in: dict[int, float | None]


@dataclass(frozen=True)
class NbpBlock:
    station: str
    version: str
    issue: datetime
    fhrs: list[int]
    periods: list[Period24] = field(default_factory=list)


def _num(text: str) -> int | None:
    text = text.strip()
    if not text or not re.fullmatch(r"-?\d+", text):
        return None
    value = int(text)
    return None if value < 0 else value  # negative codes mean missing


def parse_header(line: str) -> tuple[str, str, str, datetime]:
    m = _HEADER_RE.match(line)
    if not m:
        raise ValueError(f"not an NBM block header: {line[:60]!r}")
    issue = datetime(
        int(m["year"]), int(m["mon"]), int(m["day"]),
        int(m["hhmm"][:2]), int(m["hhmm"][2:]), tzinfo=UTC,
    )  # fmt: skip
    return m["station"], m["version"], m["product"], issue


def parse_nbp_block(text: str) -> NbpBlock:
    """Parse one station's NBP block.

    Column positions come from the FHR line, never from fixed indices: the day
    groups (separated by '|') start at a different column in each cycle.
    """
    lines = [ln.rstrip("\n") for ln in text.split("\n") if ln.strip()]
    station, version, product, issue = parse_header(lines[0])
    if product != "NBP":
        raise ValueError(f"expected an NBP block, got {product}")
    fhr_line = next((ln for ln in lines if ln.lstrip().startswith("FHR")), None)
    if fhr_line is None:
        raise ValueError(f"no FHR line in block for {station}")
    body_start = LABEL_WIDTH
    # Day groups: spans between '|' characters, each holding one or two columns.
    groups: list[tuple[int, int, int]] = []  # (span start, span end, fhr of last column)
    fhrs: list[int] = []
    pos = body_start
    for part in fhr_line[body_start:].split("|"):
        nums = [int(x) for x in part.split()]
        if nums:
            fhrs.extend(nums)
            groups.append((pos, pos + len(part), nums[-1]))
        pos += len(part) + 1
    rows = {ln[:body_start].strip(): ln for ln in lines if re.match(r"^ ?[A-Z0-9]{5} ", ln)}

    def group_values(label: str, scale: float) -> list[float | None]:
        ln = rows.get(label)
        if ln is None:
            return [None] * len(groups)
        out: list[float | None] = []
        for start, end, _ in groups:
            n = _num(ln[start:end])
            out.append(None if n is None else n / scale)
        return out

    snow = {p: group_values(f"S24{k}", 10.0) for k, p in PERCENTILES.items()}
    qpf = {p: group_values(f"Q24{k}", 100.0) for k, p in PERCENTILES.items()}
    ice = {p: group_values(f"I24{k}", 100.0) for k, p in PERCENTILES.items()}
    periods = []
    for i, (_, _, fhr) in enumerate(groups):
        valid_end = issue + timedelta(hours=fhr)
        if all(snow[p][i] is None for p in snow) and all(qpf[p][i] is None for p in qpf):
            continue  # a lone column with no 24-hour values (start or end of the table)
        periods.append(
            Period24(
                fhr=fhr,
                valid_end=valid_end,
                snow_in={p: snow[p][i] for p in snow},
                qpf_in={p: qpf[p][i] for p in qpf},
                ice_in={p: ice[p][i] for p in ice},
            )
        )
    return NbpBlock(station=station, version=version, issue=issue, fhrs=fhrs, periods=periods)


@dataclass(frozen=True)
class NbsBlock:
    """Short-range (NBS) block: only what the composite needs, 6-hour snowfall."""

    station: str
    version: str
    issue: datetime
    s06_in: dict[datetime, float | None]  # end of each 6-hour period -> inches


def parse_nbs_block(text: str) -> NbsBlock:
    """Parse one NBS block. Values are 3-character fields right-aligned under the FHR
    column they belong to (fields can touch, e.g. "57100100"), so slice by position."""
    lines = [ln.rstrip("\n") for ln in text.split("\n") if ln.strip()]
    station, version, product, issue = parse_header(lines[0])
    if product != "NBS":
        raise ValueError(f"expected an NBS block, got {product}")
    fhr_line = next((ln for ln in lines if ln.lstrip().startswith("FHR")), None)
    if fhr_line is None:
        raise ValueError(f"no FHR line in block for {station}")
    columns = [(m.end(), int(m.group())) for m in re.finditer(r"\d+", fhr_line)]
    s06_line = next((ln for ln in lines if ln[:LABEL_WIDTH].strip() == "S06"), "")
    s06: dict[datetime, float | None] = {}
    for end, fhr in columns:
        field_text = s06_line[max(end - 3, LABEL_WIDTH) : end]
        if not field_text.strip():
            continue  # S06 is only given every other column (6-hourly)
        n = _num(field_text)
        s06[issue + timedelta(hours=fhr)] = None if n is None else n / 10.0
    return NbsBlock(station=station, version=version, issue=issue, s06_in=s06)


def split_blocks(data: bytes) -> dict[str, tuple[int, int]]:
    """Station -> (offset, length) for every block in a full bulletin file."""
    heads = [(m.start(), m.group(1).decode()) for m in _ANY_HEADER_RE.finditer(data)]
    out: dict[str, tuple[int, int]] = {}
    for i, (offset, station) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(data)
        out.setdefault(station, (offset, end - offset))
    return out


# ---------- archive access ----------


@dataclass
class FetchedBlock:
    station: str
    cycle: datetime
    last_modified: datetime | None
    text: str | None  # None when the cycle or the station is absent from the archive
    how: str  # "range" | "full" | "missing"


class NbmTextArchive:
    """Fetch selected stations' blocks from archived bulletins with Range requests."""

    def __init__(
        self,
        product: str = "nbp",
        *,
        workers: int = 12,
        search_bytes: int | None = None,
        timeout_s: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if product not in PRODUCTS:
            raise ValueError(f"unknown product {product}")
        self.product = product
        self.workers = workers
        # NBP offsets are fixed; NBS offsets drift, so search a window around them.
        self.search_bytes = (
            (0 if product == "nbp" else 24_000) if search_bytes is None else search_bytes
        )
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": "pmw-research/0.1"},
            transport=transport,
            limits=httpx.Limits(max_connections=workers + 2),
        )
        # Last known (offset, length) per (cycle hour, station). NBS files list stations
        # in a different order at different cycle hours, so offsets are per hour.
        self.offsets: dict[tuple[int, str], tuple[int, int]] = {}
        self.full_downloads = 0
        self.range_requests = 0

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET with retries on dropped connections and 5xx (a long pull must survive blips)."""
        last: Exception | None = None
        for attempt in range(5):
            try:
                resp = self._client.get(url, headers=headers)
                if resp.status_code < 500:
                    return resp
                last = httpx.HTTPStatusError("server error", request=resp.request, response=resp)
            except httpx.TransportError as exc:
                last = exc
            time.sleep(min(30.0, 2.0**attempt))
        assert last is not None
        raise last

    @staticmethod
    def _last_modified(resp: httpx.Response) -> datetime | None:
        raw = resp.headers.get("last-modified")
        return parsedate_to_datetime(raw).astimezone(UTC) if raw else None

    def _range(self, url: str, hour: int, station: str) -> tuple[str | None, datetime | None, int]:
        """Fetch a window around the remembered offset and find the station's block in it.

        Returns (block text or None, last-modified, status). NBP blocks sit at the
        same offset every cycle; NBS blocks drift by a few hundred bytes a day, so
        the window is widened by `search_bytes` on each side and the found offset
        is remembered, which tracks the drift.
        """
        offset, length = self.offsets[(hour, station)]
        start = max(0, offset - self.search_bytes)
        stop = offset + max(length, RANGE_BYTES) + self.search_bytes - 1
        self.range_requests += 1
        resp = self._get(url, headers={"Range": f"bytes={start}-{stop}"})
        if resp.status_code == 404:
            return None, None, 404
        if resp.status_code == 416:
            return None, None, 409  # offset past the end of a shorter file: offsets moved
        if resp.status_code not in (200, 206):
            resp.raise_for_status()
        data = resp.content
        heads = list(_ANY_HEADER_RE.finditer(data))
        for k, m in enumerate(heads):
            if m.group(1).decode() != station:
                continue
            if k + 1 < len(heads):
                end = heads[k + 1].start()
            elif start + len(data) >= int(
                resp.headers.get("content-range", "/0").rsplit("/", 1)[-1] or 0
            ):
                end = len(data)  # last block in the file
            else:
                break  # block runs past the window: treat as moved
            self.offsets[(hour, station)] = (start + m.start(), end - m.start())
            return data[m.start() : end].decode("ascii", "replace"), self._last_modified(resp), 206
        return None, self._last_modified(resp), 409  # not in the window: offsets moved

    def _full(self, url: str) -> tuple[bytes | None, datetime | None]:
        self.full_downloads += 1
        resp = self._get(url)
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        return resp.content, self._last_modified(resp)

    def fetch_cycle(self, cycle: datetime, stations: list[str]) -> list[FetchedBlock]:
        url = bulletin_url(self.product, cycle)
        hour = cycle.hour
        known = [s for s in stations if (hour, s) in self.offsets]
        results: dict[str, FetchedBlock] = {}
        moved = len(known) < len(stations)
        if known:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for station, (text, lm, status) in zip(
                    known, pool.map(lambda s: self._range(url, hour, s), known), strict=True
                ):
                    if status == 206 and text is not None:
                        results[station] = FetchedBlock(station, cycle, lm, text, "range")
                    elif status == 404:
                        results[station] = FetchedBlock(station, cycle, None, None, "missing")
                    else:
                        moved = True
        cycle_absent = bool(results) and all(r.how == "missing" for r in results.values())
        if moved and not cycle_absent:
            data, lm = self._full(url)
            if data is None:
                for s in stations:
                    results.setdefault(s, FetchedBlock(s, cycle, None, None, "missing"))
            else:
                blocks = split_blocks(data)
                self.offsets.update({(hour, s): blocks[s] for s in stations if s in blocks})
                for s in stations:
                    if s in results and results[s].how == "range":
                        continue
                    if s in blocks:
                        o, n = blocks[s]
                        text = data[o : o + n].decode("ascii", "replace")
                        results[s] = FetchedBlock(s, cycle, lm, text, "full")
                    else:
                        results[s] = FetchedBlock(s, cycle, lm, None, "missing")
        for s in stations:
            results.setdefault(s, FetchedBlock(s, cycle, None, None, "missing"))
        return [results[s] for s in stations]


def cycles_for_day(day: date, hours: list[int]) -> list[datetime]:
    return [datetime(day.year, day.month, day.day, h, tzinfo=UTC) for h in hours]


def day_path(root: Path, product: str, day: date) -> Path:
    return root / product / f"{day:%Y}" / f"{day:%Y%m%d}.jsonl.gz"


def pull_day(
    archive: NbmTextArchive, root: Path, day: date, stations: list[str], hours: list[int]
) -> tuple[int, int]:
    """Fetch one UTC day of cycles and write it atomically. Returns (blocks, missing)."""
    path = day_path(root, archive.product, day)
    if path.exists():
        return 0, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = missing = 0
    tmp = path.with_suffix(".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for cycle in cycles_for_day(day, hours):
            for b in archive.fetch_cycle(cycle, stations):
                blocks += b.text is not None
                missing += b.text is None
                f.write(
                    json.dumps(
                        {
                            "product": archive.product,
                            "station": b.station,
                            "cycle": b.cycle.isoformat(),
                            "last_modified": b.last_modified.isoformat()
                            if b.last_modified
                            else None,
                            "how": b.how,
                            "text": b.text,
                        }
                    )
                    + "\n"
                )
    tmp.rename(path)
    return blocks, missing


def read_day(root: Path, product: str, day: date) -> Iterator[dict[str, object]]:
    path = day_path(root, product, day)
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)
