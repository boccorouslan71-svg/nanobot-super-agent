#!/usr/bin/env python3
"""Generate or edit an image with Agnes AI (agnes-image-2.1-flash).

Prints one JSON line describing the result. Fails loudly: a missing key, an
HTTP error, or a response without an image all exit non-zero with the reason,
because a silent empty result is indistinguishable from success to a caller.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://apihub.agnes-ai.com/v1/images/generations"
MODEL = "agnes-image-2.1-flash"
KEY_VARS = ("AGNES_IMAGE_API_KEY", "AGNES_API_KEY")
RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")


def die(message: str) -> "None":
    print(f"agnes-image: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_key() -> str:
    for var in KEY_VARS:
        value = (os.environ.get(var) or "").strip()
        if value and not value.startswith("${"):
            return value
    local = Path(__file__).with_name("config.json")
    if local.is_file():
        try:
            data = json.loads(local.read_text())
        except json.JSONDecodeError as exc:
            die(f"{local} is not valid JSON: {exc}")
        for var in KEY_VARS:
            value = (data.get(var) or "").strip()
            if value:
                return value
    die(f"no API key: set one of {', '.join(KEY_VARS)} in the environment")
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agnes AI image generation")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--size", default="1K", help="tier (1K/2K/3K/4K) or WxH")
    parser.add_argument("--ratio", default="1:1", choices=RATIOS)
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="reference image URL or data URI; repeat for multi-image composition",
    )
    parser.add_argument("--out", help="also download the result to this path")
    parser.add_argument("--base64", action="store_true", help="ask for base64 instead of a URL")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    extra: dict[str, object] = {
        "ratio": args.ratio,
        "response_format": "b64_json" if args.base64 else "url",
    }
    if args.image:
        extra["image"] = args.image

    payload = {
        "model": MODEL,
        "prompt": args.prompt,
        "size": args.size,
        "extra_body": extra,
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {resolve_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        die(f"HTTP {exc.code} from the API: {detail}")
    except urllib.error.URLError as exc:
        die(f"cannot reach the API: {exc.reason}")

    if isinstance(body.get("error"), (str, dict)):
        die(f"API error: {json.dumps(body['error'])[:400]}")
    items = body.get("data") or []
    if not items:
        die(f"no image in the response: {json.dumps(body)[:400]}")

    first = items[0]
    url = first.get("url")
    b64 = first.get("b64_json")
    if not url and not b64:
        die(f"response carried neither a url nor base64 data: {json.dumps(first)[:300]}")

    out_path = None
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if b64:
            out_path.write_bytes(base64.b64decode(b64))
        else:
            with urllib.request.urlopen(url, timeout=args.timeout) as src:
                out_path.write_bytes(src.read())
        if out_path.stat().st_size == 0:
            die(f"downloaded file is empty: {out_path}")

    print(
        json.dumps(
            {
                "url": url,
                "path": str(out_path) if out_path else None,
                "size": args.size,
                "ratio": args.ratio,
                "model": MODEL,
                "inputs": len(args.image),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
