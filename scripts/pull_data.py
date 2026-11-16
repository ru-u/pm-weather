"""Copy the logger bucket to ./data/bucket (incremental: skips files already present).

Run with `make pull-data` (everything) or `make pull-data PREFIX=raw/ercot`. Needs PMW_S3_BUCKET, PMW_S3_ENDPOINT,
PMW_S3_ACCESS_KEY_ID and PMW_S3_SECRET_ACCESS_KEY in the environment. These are
bucket credentials only; this script never touches trading keys.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pmw.utils.storage import Storage


def main() -> int:
    if not os.environ.get("PMW_S3_BUCKET"):
        print("PMW_S3_BUCKET is not set; nothing to pull.", file=sys.stderr)
        return 1
    remote = Storage.from_env()
    dest = Path(os.environ.get("PMW_PULL_DIR", "data/bucket"))
    copied = skipped = n_bytes = 0
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    for f in remote.list(prefix):
        target = dest / f.path
        if target.exists() and target.stat().st_size == f.size:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(remote.read_bytes(f.path))
        tmp.rename(target)
        copied += 1
        n_bytes += f.size
    print(f"pulled {copied} files ({n_bytes / 1e6:.1f} MB), {skipped} already present -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
