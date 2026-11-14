"""Typed config loading for configs/*.yaml."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


def config_dir() -> Path:
    env = os.environ.get("PMW_CONFIG_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "configs").is_dir():
            return parent / "configs"
    return Path("configs")


def _load(name: str) -> dict[str, Any]:
    with open(config_dir() / name, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a mapping")
    return data


# ---------- series.yaml ----------


class SeriesGroup(BaseModel):
    """A set of Kalshi series we track, defined by regexes, never by hardcoded tickers."""

    description: str = ""
    match: list[str]  # regexes; matched against "TICKER | title | tag1, tag2"
    exclude: list[str] = Field(default_factory=list)
    hot_close_within_hours: float = 0.0  # 0 disables "hot because it closes soon"
    always_hot: bool = False

    @field_validator("match", "exclude")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        for p in patterns:
            re.compile(p)
        return patterns

    def matches(self, haystack: str) -> bool:
        if not any(re.search(p, haystack, re.IGNORECASE) for p in self.match):
            return False
        return not any(re.search(p, haystack, re.IGNORECASE) for p in self.exclude)


class KalshiSeriesConfig(BaseModel):
    categories: list[str]
    groups: dict[str, SeriesGroup]  # order matters: first matching group wins

    def classify(self, ticker: str, title: str, tags: list[str] | None) -> str | None:
        haystack = f"{ticker} | {title} | {', '.join(tags or [])}"
        for name, group in self.groups.items():
            if group.matches(haystack):
                return name
        return None


class SeriesConfig(BaseModel):
    kalshi: KalshiSeriesConfig


# ---------- logger.yaml ----------


class KalshiClientConfig(BaseModel):
    base_url: str
    min_request_interval_s: float = 0.25
    timeout_s: float = 20.0
    max_attempts: int = 6
    user_agent: str = "pmw-research-logger/0.1"


class Cadence(BaseModel):
    orderbook_base_s: int = 300
    orderbook_hot_s: int = 60
    markets_s: int = 300
    discovery_s: int = 3600
    flush_s: int = 600
    flush_max_rows: int = 20000


class HotConfig(BaseModel):
    volume_spike_contracts: float = 200.0
    volume_hot_hold_s: int = 3600


class BurstConfig(BaseModel):
    """UTC windows around major model releases when burst groups go to hot cadence."""

    groups: list[str] = Field(default_factory=list)
    windows_utc: list[tuple[str, str]] = Field(default_factory=list)


class TradesConfig(BaseModel):
    startup_lookback_h: float = 24.0


class RawConfig(BaseModel):
    enabled: bool = True
    retention_days: int = 14


class LoggerConfig(BaseModel):
    kalshi: KalshiClientConfig
    cadence: Cadence = Field(default_factory=Cadence)
    hot: HotConfig = Field(default_factory=HotConfig)
    bursts: BurstConfig = Field(default_factory=BurstConfig)
    trades: TradesConfig = Field(default_factory=TradesConfig)
    raw: RawConfig = Field(default_factory=RawConfig)


# ---------- stations.yaml ----------


class Station(BaseModel):
    name: str
    icao: str
    cli_id: str  # IEM CLI station id, e.g. KNYC
    cli_product: str = ""  # NWS product id, e.g. CLINYC
    wfo: str = ""
    lat: float
    lon: float
    tz: str
    std_utc_offset_hours: float
    series: list[str] = Field(default_factory=list)
    verified: str = ""  # how the station mapping was confirmed


class StationsConfig(BaseModel):
    stations: dict[str, Station]

    def for_series(self, series_ticker: str) -> Station | None:
        for st in self.stations.values():
            if series_ticker in st.series:
                return st
        return None


def load_series_config() -> SeriesConfig:
    return SeriesConfig.model_validate(_load("series.yaml"))


def load_logger_config() -> LoggerConfig:
    return LoggerConfig.model_validate(_load("logger.yaml"))


def load_stations_config() -> StationsConfig:
    return StationsConfig.model_validate(_load("stations.yaml"))
