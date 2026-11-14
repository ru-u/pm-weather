"""Series filters, checked against real series objects from Kalshi."""

from pmw.utils.config import load_logger_config, load_series_config, load_stations_config

from .conftest import load_fixture


def _classify(ticker: str) -> str | None:
    cfg = load_series_config().kalshi
    s = {x["ticker"]: x for x in load_fixture("kalshi_series_sample.json")["series"]}[ticker]
    return cfg.classify(s["ticker"], s["title"], s.get("tags"))


def test_target_series_land_in_the_right_group():
    assert _classify("KXNYCSNOWM") == "snow"
    assert _classify("KXTSNOWFALLBIGSKYM") == "snow"
    assert _classify("KXSNOWXMASBOS") == "snow"
    assert _classify("KXSNOWSTORM") == "snow_storm"
    assert _classify("KXTXERCOTPEAKD") == "ercot_demand"
    assert _classify("KXERCOTNORTHW") == "power_price"
    assert _classify("KXLOWTCHI") == "low_temp"


def test_us_low_temp_cities_with_tricky_codes_are_kept():
    for ticker in ("KXLOWTLAX", "KXLOWTMIA", "KXLOWTSATX"):
        assert _classify(ticker) == "low_temp", ticker


def test_lookalikes_are_excluded():
    assert _classify("KXRAINNYCM") is None  # tagged "Snow and rain" but a rain market
    assert _classify("KXSNOWBIRDOPEN") is None  # ski resort opening date
    assert _classify("KXLOWTEGLL") is None  # London
    assert _classify("KXLOWTSBGR") is None  # Sao Paulo
    assert _classify("KXHIGHNY") is None  # daily high
    assert _classify("KXERCOTX") is None  # renewables share
    assert _classify("KXNATGASD") is None


def test_configs_load_and_are_consistent():
    logger = load_logger_config()
    assert logger.kalshi.base_url.startswith("https://")
    assert logger.cadence.orderbook_hot_s < logger.cadence.orderbook_base_s
    series = load_series_config().kalshi
    assert set(logger.bursts.groups) <= set(series.groups)
    stations = load_stations_config()
    nyc = stations.for_series("KXNYCSNOWM")
    assert nyc is not None and nyc.cli_id == "KNYC"  # Central Park, not an airport
    for st in stations.stations.values():
        assert -10 <= st.std_utc_offset_hours <= -4
