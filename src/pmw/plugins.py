"""Plugin interface: probability models and strategies.

The public package ships baselines (`naive` strategy, `column` model). Other
packages add their own by declaring Python entry points; this package never
imports them by name:

    [project.entry-points."pmw.strategies"]
    my_strategy = "my_package.strategies:MyStrategy"
    [project.entry-points."pmw.models"]
    my_model = "my_package.models:MyModel"

Parameters come from a YAML file, chosen in this order: an explicit path
(`--params`), the `PMW_PARAMS` environment variable, else none (each class's
own defaults). The file names the strategy and model and holds keyword
arguments per name:

    strategy: naive
    model: column
    naive: {tau: 0.10, cap_market: 250, cap_event: 1000}
    column: {column: p_comp}
"""

from __future__ import annotations

import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml

if TYPE_CHECKING:
    import numpy as np
    import pandas as pd

    from pmw.backtest.taker import Entry, Exposure, Observation

STRATEGY_GROUP = "pmw.strategies"
MODEL_GROUP = "pmw.models"
PARAMS_ENV = "PMW_PARAMS"


@runtime_checkable
class Strategy(Protocol):
    """Turns one observation into at most one paper entry."""

    name: str

    def decide(self, obs: Observation, exposure: Exposure) -> Entry | None: ...


@runtime_checkable
class ProbabilityModel(Protocol):
    """P(YES) for each row of a model-specific frame of (market, decision time) rows."""

    name: str

    def prob_yes(self, rows: pd.DataFrame) -> np.ndarray: ...


class ColumnModel:
    """Baseline model: a probability already computed into the rows (e.g. the NBM composite)."""

    name = "column"

    def __init__(self, column: str = "p_comp") -> None:
        self.column = column

    def prob_yes(self, rows: pd.DataFrame) -> np.ndarray:
        import numpy as np

        return np.asarray(rows[self.column], dtype=float)


def _builtin_strategies() -> dict[str, type]:
    from pmw.strategy import NaiveTaker

    return {"naive": NaiveTaker}


def _builtin_models() -> dict[str, type]:
    return {"column": ColumnModel}


def _discovered(group: str) -> dict[str, Any]:
    return {ep.name: ep for ep in entry_points(group=group)}


def available() -> dict[str, dict[str, str]]:
    """Every strategy and model name, with where it comes from."""
    out: dict[str, dict[str, str]] = {"strategies": {}, "models": {}}
    for kind, builtins, group in (("strategies", _builtin_strategies(), STRATEGY_GROUP),
                                  ("models", _builtin_models(), MODEL_GROUP)):  # fmt: skip
        out[kind].update({k: "built-in" for k in builtins})
        out[kind].update({k: ep.value for k, ep in _discovered(group).items()})
    return out


def params_source(path: str | Path | None = None) -> str | None:
    if path:
        return str(path)
    return os.environ.get(PARAMS_ENV) or None


def load_params(path: str | Path | None = None) -> dict[str, Any]:
    """The params file's contents, or {} when none is configured."""
    src = params_source(path)
    if src is None:
        return {}
    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(
            f"params file {p} not found (from {'--params' if path else PARAMS_ENV})"
        )
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"params file {p} must hold a mapping")
    return data


def _resolve(name: str, builtins: dict[str, type], group: str, kind: str) -> type:
    if name in builtins:
        return builtins[name]
    found = _discovered(group)
    if name in found:
        cls: type = found[name].load()
        return cls
    known = sorted({*builtins, *found})
    raise KeyError(f"unknown {kind} {name!r}; available: {', '.join(known)}")


def get_strategy(name: str | None = None, params: dict[str, Any] | None = None) -> Strategy:
    params = params or {}
    name = name or params.get("strategy") or "naive"
    cls = _resolve(name, _builtin_strategies(), STRATEGY_GROUP, "strategy")
    obj: Strategy = cls(**(params.get(name) or {}))
    return obj


def get_model(name: str | None = None, params: dict[str, Any] | None = None) -> ProbabilityModel:
    params = params or {}
    name = name or params.get("model") or "column"
    cls = _resolve(name, _builtin_models(), MODEL_GROUP, "model")
    obj: ProbabilityModel = cls(**(params.get(name) or {}))
    return obj
