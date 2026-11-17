# Commitments

Some pre-registered texts, decision entries and models are kept private until
their experiment is evaluated (D-053). This file publishes their sha256 hashes
first, so each text can be revealed later and anyone can check it was not
changed after this file was pushed.

To verify a revealed file: `shasum -a 256 <file>` must print the hash listed
here, and this file's commit must predate the experiment's evaluation date.

## Pre-registration texts (full sections of `docs/preregistration.md`)

| Experiment | Frozen (UTC) | Evaluated | sha256 of the text |
|---|---|---|---|
| P4E | 2026-11-17 04:20 | 2026-11-17 (run once) | `d57914df4968862ebd651b80a750f43f33bfacd776628f0c07268e8960e3df02` |
| P4-paper | 2026-11-17 04:45 | on or after 2027-10-15 | `ad023f9f9a5253cc0a5973a1939068f264fb5641db22f420b1cea0e44e69050f` |
| P4S-T | 2026-11-17 14:25 | 2026-11-17 (run once) | `e81c0225541654cba5dc55957138b7a0a9c67c28ba222b59e18388de7ea4b0e1` |
| P4S-paper | 2026-11-17 14:25, amended same day | on or after 2027-05-15 | `df42ae563b20bd7c22df3c4eeb66b10df85b87a7fd00c387ed6a556711c31f90` |
| P4-paper implementation clarifications | 2026-11-17, before the first sample day | with P4-paper | `3c454c2f8f6c819c2e5c0e4bd9e3dfdf3cf78a990a7db0163386cf4dc0acb9f8` |

## Decision entries (full text of `docs/decisions.md` entries)

| Entry | sha256 |
|---|---|
| D-045 | `d46f3e7538b33d4760c8b2ece93dfa3c04cee14931e152bad602ce51684b4e2e` |
| D-048 | `68eded817b135416439b5faad478fb0965267627f2e406383dcf0033d4139b25` |
| D-049 | `961f8b4cf9fbbfd62102d9ad668214b2d2c388f9465519633d9482f2ca113ba2` |
| D-050 | `c93db483dcfdc0f9f9dc20ed8c4bebc49a3b930f7a604599aa307c04915a93da` |
| D-051 | `2a6da5ae42d20ca494d9a1a555729331335f4af90afda38c91b59819b09b9377` |
| D-052 | `0980fbe77a849c6bdc1cc4af9285c8fb07b523e0a0a315d074354c40029fb2d6` |

## Frozen models (sha256 of the pickled object)

| Model | Used by | sha256 |
|---|---|---|
| ERCOT nowcast v2 | P4E, P4-paper | `816ab8652eb46a59ce38f4f2f28e63e6926461b811b939c7949df4dd3dad48d9` |
| Snow nowcast v1 | P4S-T, P4S-paper secondary arm | `78867cd2f1abd34c34dbf344f59ee4fd14eb39d6c99c16422c6e87294f012b93` |
| Snow nowcast v1.1 | P4S-paper primary | `d7c816dce2915a83126572ae2ce4a2822918c1daeaf36405ad56f9c574080c64` |

## Full history before the split

The commit history up to the split, including every private file with its
original timestamps, ends at commit
`1ab8dfa3c5d422bba0538effb3a801aeb278d35d` (2026-11-17). It is kept privately
and can be published after the evaluations; a git commit hash commits to the
whole tree and history beneath it.
