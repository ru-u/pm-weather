"""Forest plot of every registered accuracy comparison (README figure).

Each row is D = Brier(baseline) - Brier(challenger) with its 95% block-bootstrap
interval, so positive means the challenger was more accurate. Numbers are copied
from the result files cited per row; nothing is recomputed here.

    uv run python scripts/results_figure.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path("docs/findings/figures/registered_tests.png")
INK, MUTED, GRID, SURFACE, SERIES = "#0b0b0b", "#52514e", "#d9d8d3", "#fcfcfb", "#2a78d6"

# (label, D, ci_low, ci_high, source)
MARKET = [
    ("P2A  snow: NBM composite vs Kalshi market", -0.0103, -0.0167, -0.0029,
     "docs/findings/2026-11-15-p2a-result.md"),
    ("P3A  ERCOT: ERCOT load forecast vs Kalshi market", -0.0110, -0.0178, -0.0035,
     "docs/findings/2026-11-16-p3a-result.md"),
    ("P4E  ERCOT: frozen nowcast v2 vs Kalshi market", -0.0053, -0.0175, +0.0086,
     "docs/preregistration.md (Results)"),
]  # fmt: skip
MODEL = [
    ("P2B  snow: recalibrated NBM vs raw NBM", -0.0001, -0.0017, +0.0014,
     "docs/findings/2026-11-17-p2b-result.md"),
    ("ERCOT nowcast v2 vs v1 (validation)", +0.0011, +0.0005, +0.0017,
     "docs/findings/2026-11-16-phase4-ercot-v2.md"),
    ("P4S-T  snow: frozen nowcast vs NBM composite", +0.0081, +0.0057, +0.0109,
     "docs/preregistration.md (Results)"),
]  # fmt: skip


def main() -> None:
    from matplotlib.transforms import blended_transform_factory

    plt.rcParams.update({"font.size": 9, "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(9.6, 4.2), dpi=170, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    right = blended_transform_factory(ax.transAxes, ax.transData)

    ticks: list[float] = []
    labels: list[str] = []
    headers: list[int] = []
    y = 0.0
    for title, group in (("Against the market (real quotes)", MARKET),
                         ("Model against a free baseline (no prices)", MODEL)):  # fmt: skip
        headers.append(len(ticks))
        ticks.append(y)
        labels.append(title)
        y -= 1.0
        for label, d, lo, hi, _ in group:
            excludes_zero = lo > 0 or hi < 0
            ax.plot([lo, hi], [y, y], color=SERIES, lw=2, solid_capstyle="round", zorder=2)
            ax.plot([d], [y], marker="o", ms=8, mew=2, color=SERIES,
                    mfc=SERIES if excludes_zero else SURFACE, zorder=3)  # fmt: skip
            ax.text(1.02, y, f"{d:+.4f}  [{lo:+.4f}, {hi:+.4f}]", transform=right,
                    ha="left", va="center", color=MUTED, family="DejaVu Sans Mono",
                    fontsize=8)  # fmt: skip
            ticks.append(y)
            labels.append(label)
            y -= 1.0
        y -= 0.4

    ax.set_yticks(ticks, labels)
    for k, t in enumerate(ax.get_yticklabels()):
        t.set_color(INK)
        if k in headers:
            t.set_fontweight("bold")
    ax.tick_params(axis="y", length=0, pad=8)
    ax.axvline(0, color=MUTED, lw=1, zorder=1)
    for t in (-0.015, -0.01, -0.005, 0.005, 0.01):
        ax.axvline(t, color=GRID, lw=0.6, zorder=0)
    ax.set_xlim(-0.02, 0.013)
    ax.set_ylim(y + 0.5, 0.6)
    ax.set_xticks([-0.015, -0.01, -0.005, 0, 0.005, 0.01])
    ax.set_xticklabels(["\u22120.015", "\u22120.010", "\u22120.005", "0", "+0.005", "+0.010"])
    ax.tick_params(axis="x", colors=GRID, labelcolor=MUTED, length=3)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.set_xlabel(
        "D = Brier(baseline) \u2212 Brier(challenger), with 95% block-bootstrap interval.  "
        "Left of 0: baseline more accurate.\nRight of 0: challenger more accurate.  "
        "Filled dot: interval excludes 0.  Each row names challenger vs baseline.",
        color=MUTED, fontsize=8,
    )  # fmt: skip
    ax.text(1.02, 0.6, "D  [95% interval]", transform=right, ha="left", va="center",
            color=INK, fontweight="bold", fontsize=8)  # fmt: skip
    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.15)
    print(OUT)


if __name__ == "__main__":
    main()
