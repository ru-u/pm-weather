# P3A result tables

Verdict under the registered rule: **CONDITIONAL**

D = Brier(market) - Brier(ERCOT forecast); positive means the ERCOT forecast was more accurate.

| | n | weeks | D | 95% CI |
| - | -: | -: | -: | - |
| **Primary** | 1551 | 12 | -0.0110 | [-0.0178, -0.0035] |
| Spread <= 0.1 | 897 | 12 | -0.0066 | [-0.0118, +0.0012] |
| Spread <= 0.3 | 1875 | 12 | -0.0145 | [-0.0210, -0.0077] |
| Training on last 3 years | 1551 | 12 | -0.0102 | [-0.0181, -0.0013] |
| Decision 00CT | 148 | 9 | -0.0143 | [-0.0245, -0.0029] |
| Decision 06CT | 579 | 12 | -0.0072 | [-0.0159, +0.0059] |
| Decision 10CT | 569 | 12 | -0.0111 | [-0.0167, -0.0047] |
| Decision 14CT | 255 | 12 | -0.0176 | [-0.0405, +0.0017] |
| Top 20% peak days | 247 | 5 | -0.0054 | [-0.0400, +0.0148] |
| 17:00 CT (secondary) | 74 | 9 | -0.1866 | [-0.3316, -0.1087] |

| Forecast | Brier | Log loss |
| - | -: | -: |
| Kalshi market | 0.0557 | 0.2077 |
| ERCOT forecast | 0.0667 | 0.2286 |

Murphy decomposition (reliability, resolution, uncertainty):

- market: 0.0021, 0.1910, 0.2451
- ercot: 0.0027, 0.1799, 0.2451

| Taker tau | trades | contracts | P&L after fees | fees | share of resamples > 0 | 95% interval | perceived/realized corr |
| -: | -: | -: | -: | -: | -: | - | -: |
| 0.05 | 62 | 2440 | $156 | $19 | 0.809 | [$-174, $525] | -0.19 |
| 0.1 | 54 | 2249 | $261 | $17 | 0.942 | [$-51, $639] | -0.33 |
| 0.15 | 40 | 1730 | $75 | $13 | 0.667 | [$-237, $418] | -0.20 |

Capacity: median contracts per stressed day 37,855 (gate 20,000: met); all days median 18,568; approximate premium on stressed days $16,810. Stressed days: 2026-08-28, 2026-08-29, 2026-08-30, 2026-08-31, 2026-09-02, 2026-09-07, 2026-09-08, 2026-09-09, 2026-09-11, 2026-09-12, 2026-09-13, 2026-09-14, 2026-09-15, 2026-09-16, 2026-09-21, 2026-09-22.

Leakage check: {'posting_not_before_decision': 0, 'training_day_in_test': 0}. Markets scored: 785; operating days: 76; training ratios: {'ratios': 16712, 'ratios_recent': 5475}.

Reliability (bin, n, mean forecast, observed frequency):

- market: 0.05->0.02 (n=389); 0.13->0.06 (n=168); 0.25->0.23 (n=40); 0.35->0.32 (n=28); 0.45->0.41 (n=32); 0.55->0.52 (n=31); 0.65->0.76 (n=45); 0.75->0.78 (n=36); 0.86->0.92 (n=185); 0.95->0.98 (n=597)
- ercot: 0.02->0.02 (n=393); 0.15->0.08 (n=85); 0.25->0.22 (n=67); 0.35->0.27 (n=67); 0.46->0.40 (n=53); 0.55->0.60 (n=53); 0.65->0.74 (n=66); 0.75->0.79 (n=98); 0.86->0.95 (n=164); 0.95->1.00 (n=505)
