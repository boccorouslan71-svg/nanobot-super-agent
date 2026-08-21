#!/usr/bin/env python3
"""Generate an image with Cloudflare Workers AI and write it to disk.

Handles both response shapes Cloudflare uses (base64 JSON for FLUX, raw PNG for
the Stable Diffusion models) and verifies the written file is non-empty, so a
reported success always means there are real pixels on disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODELS = {
    "flux-schnell": "@cf/black-forest-labs/flux-1-schnell",
    "sdxl-lightning": "@cf/bytedance/stable-diffusion-xl-lightning",
    "sdxl-base": "@cf/stabilityai/stable-diffusion-xl-base-1.0",
    "dreamshaper-8": "@cf/lykon/dreamshaper-8-lcm",
}


def die(message: str) -> "None":
    print(f"cloudflare-ai-image: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_credentials() -> tuple[str, str]:
    token = (os.environ.get("CF_API_TOKEN") or "").strip()
    account = (os.environ.get("CF_ACCOUNT_ID") or "").strip()
    if not token or not account or token.startswith("${"):
        local = Path(__file__).with_name("config.json")
        if local.is_file():
            try:
                data = json.loads(local.read_text())
            except json.JSONDecodeError as exc:
                die(f"{local} is not valid JSON: {exc}")
            token = token or (data.get("CF_API_TOKEN") or "").strip()
            account = account or (data.get("CF_ACCOUNT_ID") or "").strip()
    if not token:
        die("no CF_API_TOKEN in the environment or scripts/config.json")
    if not account:
        die("no CF_ACCOUNT_ID in the environment or scripts/config.json")
    return token, account


def slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:40] or "image").rstrip("-")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloudflare Workers AI image generation")
    parser.add_argument("--prompt")
    parser.add_argument("--model", default="flux-schnell", choices=sorted(MODELS))
    parser.add_argument("--out")
    parser.add_argument("--steps", type=int, default=4, help="FLUX only, 1-8")
    parser.add_argument("--negative", help="SD models only")
    parser.add_argument("--width", type=int, help="SD models only")
    parser.add_argument("--height", type=int, help="SD models only")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args()

    if args.list_models:
        print(json.dumps(MODELS, indent=2))
        return 0
    if not args.prompt:
        die("--prompt is required (or use --list-models)")

    token, account = resolve_credentials()
    model = MODELS[args.model]
    payload: dict[str, object] = {"prompt": args.prompt}
    if args.model == "flux-schnell":
        payload["steps"] = max(1, min(8, args.steps))
    else:
        if args.negative:
            payload["negative_prompt"] = args.negative
        if args.width:
            payload["width"] = args.width
        if args.height:
            payload["height"] = args.height

    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read()
    except urllib.error.HTTPError as exc:
        die(f"HTTP {exc.code} from Cloudflare: {exc.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as exc:
        die(f"cannot reach Cloudflare: {exc.reason}")

    if "application/json" in content_type:
        body = json.loads(raw.decode())
        if not body.get("success", True) or body.get("errors"):
            die(f"Cloudflare reported failure: {json.dumps(body.get('errors'))[:400]}")
        encoded = (body.get("result") or {}).get("image")
        if not encoded:
            die(f"no image in the response: {json.dumps(body)[:300]}")
        data = base64.b64decode(encoded)
    else:
        data = raw
    if not data:
        die("provider returned zero bytes")

    # Cloudflare answers with JPEG bytes for some models and PNG for others,
    # regardless of the model family, so the extension follows the actual magic
    # bytes. A .png file holding JPEG data breaks downstream consumers that
    # trust the name.
    detected = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
    if args.out:
        out_path = Path(args.out)
        if out_path.suffix.lower().lstrip(".") not in {detected, "jpeg" if detected == "jpg" else ""}:
            out_path = out_path.with_suffix(f".{detected}")
    else:
        out_path = Path("output") / f"{slug(args.prompt)}.{detected}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    written = out_path.stat().st_size
    if written == 0:
        die(f"wrote an empty file: {out_path}")

    print(json.dumps({"path": str(out_path), "bytes": written, "model": model}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
