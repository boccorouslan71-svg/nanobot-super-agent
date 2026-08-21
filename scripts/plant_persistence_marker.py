"""Plant a marker file into the mirrored tree, to prove the restore path end to end.

The snapshot half of the mirror is already proven by the live row (the container
uploaded its own tree). What still needs proof is the restore half: that a file
which exists ONLY in Supabase — never in the Docker image — is put back on disk
when the container is recycled.

Usage: python3 scripts/plant_persistence_marker.py <marker-relative-path>
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import sys
import tarfile
from datetime import datetime, timezone
from typing import Any

import httpx

TABLE = "nanobot_state_blobs"
KEY = "state/data-tree"


def _headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    rel = argv[1].lstrip("/")
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    endpoint = f"{url}/rest/v1/{TABLE}"

    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            endpoint,
            headers=_headers(key),
            params={"select": "content", "key": f"eq.{KEY}", "limit": "1"},
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            print(f"no '{KEY}' row to amend")
            return 1
        content: dict[str, Any] = rows[0]["content"]
        archive = base64.b64decode(content["data"])

        stamp = datetime.now(timezone.utc).isoformat()
        body = (
            "# Persistence proof\n\n"
            f"Written into the Supabase mirror at {stamp}.\n\n"
            "This file exists only in the durable mirror — it was never part of the\n"
            "container image. Seeing it on disk after a restart proves the restore\n"
            "path works, which is what used to be missing.\n"
        ).encode("utf-8")

        raw = io.BytesIO()
        names: list[str] = []
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as src, tarfile.open(
            fileobj=raw, mode="w", format=tarfile.PAX_FORMAT
        ) as dst:
            for member in src.getmembers():
                if member.name == rel:
                    continue
                extracted = src.extractfile(member)
                if extracted is None:
                    continue
                dst.addfile(member, io.BytesIO(extracted.read()))
                names.append(member.name)
            marker = tarfile.TarInfo(name=rel)
            marker.size = len(body)
            marker.mtime = 0
            marker.mode = 0o644
            dst.addfile(marker, io.BytesIO(body))
            names.append(rel)

        rebuilt = gzip.compress(raw.getvalue(), compresslevel=6, mtime=0)
        payload = {
            "format": "tar.gz+base64",
            "data": base64.b64encode(rebuilt).decode("ascii"),
            "file_count": len(names),
            "byte_size": len(rebuilt),
            "created_at": stamp,
        }
        write = client.post(
            endpoint,
            headers={**_headers(key), "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "key"},
            json={"key": KEY, "content": payload, "content_hash": "manual-marker"},
        )
        write.raise_for_status()

    print(json.dumps({"planted": rel, "file_count": len(names), "byte_size": len(rebuilt)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
