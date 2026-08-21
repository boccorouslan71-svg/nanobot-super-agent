#!/usr/bin/env python3
"""Generate an image through Hugging Face Inference Providers.

The provider that serves a given model changes over time, so the mapping is read
from the model metadata instead of being hardcoded. Every failure is reported
with the provider's own message: this route breaks most often because of token
permissions, and a generic error would send the caller looking in the wrong
place.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ALIASES = {
    "z-image-turbo": "Tongyi-MAI/Z-Image-Turbo",
    "flux-schnell": "black-forest-labs/FLUX.1-schnell",
    "flux-dev": "black-forest-labs/FLUX.1-dev",
    "sd-3.5-large": "stabilityai/stable-diffusion-3.5-large",
    "sdxl-base": "stabilityai/stable-diffusion-xl-base-1.0",
}
DEFAULT_MODEL = "z-image-turbo"


def die(message: str) -> "None":
    print(f"hugging-face-image: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_key() -> str:
    for var in ("HF_API_KEY", "HF_TOKEN", "HUGGINGFACE_API_KEY"):
        value = (os.environ.get(var) or "").strip()
        if value and not value.startswith("${"):
            return value
    local = Path(__file__).with_name("config.json")
    if local.is_file():
        try:
            data = json.loads(local.read_text())
        except json.JSONDecodeError as exc:
            die(f"{local} is not valid JSON: {exc}")
        for var in ("HF_API_KEY", "HF_TOKEN"):
            if (data.get(var) or "").strip():
                return data[var].strip()
    die("no HF_API_KEY in the environment or scripts/config.json")
    raise AssertionError("unreachable")


def get_json(url: str, key: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def live_providers(model: str, key: str) -> list[tuple[str, str]]:
    url = f"https://huggingface.co/api/models/{model}?expand[]=inferenceProviderMapping"
    try:
        mapping = get_json(url, key).get("inferenceProviderMapping") or {}
    except urllib.error.HTTPError as exc:
        die(f"cannot read model metadata for {model}: HTTP {exc.code}")
    entries = mapping.items() if isinstance(mapping, dict) else [
        (m.get("provider", "?"), m) for m in mapping
    ]
    return [
        (name, info.get("providerId", model))
        for name, info in entries
        if isinstance(info, dict) and info.get("status") == "live"
    ]


def generate(model: str, provider: str, prompt: str, key: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        f"https://router.huggingface.co/{provider}/v1/images/generations",
        data=json.dumps({"model": model, "prompt": prompt, "response_format": "b64_json"}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if raw[:1] in (b"{", b"["):
        import base64

        body = json.loads(raw.decode())
        items = body.get("data") or []
        if not items:
            raise RuntimeError(f"no image in the response: {json.dumps(body)[:300]}")
        first = items[0]
        if first.get("b64_json"):
            return base64.b64decode(first["b64_json"])
        if first.get("url"):
            with urllib.request.urlopen(first["url"], timeout=timeout) as src:
                return src.read()
        raise RuntimeError(f"unexpected item shape: {json.dumps(first)[:300]}")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Hugging Face image generation")
    parser.add_argument("--prompt")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--provider", help="force one provider instead of trying the live ones")
    parser.add_argument("--out")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--check", action="store_true", help="diagnose token permissions")
    args = parser.parse_args()

    key = resolve_key()
    model = ALIASES.get(args.model, args.model)

    if args.check:
        try:
            who = get_json("https://huggingface.co/api/whoami-v2", key)
        except urllib.error.HTTPError as exc:
            die(f"token rejected by Hugging Face: HTTP {exc.code}")
        token = (who.get("auth") or {}).get("accessToken") or {}
        report = {
            "user": who.get("name"),
            "token": token.get("displayName"),
            "role": token.get("role"),
            "model": model,
            "providers": {},
        }
        for name, _ in live_providers(model, key) or [("(none live)", "")]:
            if not _:
                report["providers"][name] = "no live provider in model metadata"
                continue
            try:
                generate(model, name, "permission probe", key, 60)
                report["providers"][name] = "ok"
            except urllib.error.HTTPError as exc:
                report["providers"][name] = (
                    f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:120]}"
                )
            except Exception as exc:  # noqa: BLE001 - surfaced verbatim in the report
                report["providers"][name] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(report, indent=2))
        return 0 if "ok" in report["providers"].values() else 1

    if not args.prompt:
        die("--prompt is required (or use --check)")

    candidates = [(args.provider, model)] if args.provider else live_providers(model, key)
    if not candidates:
        die(f"no live inference provider for {model}")

    failures: list[str] = []
    data = b""
    used = ""
    for name, _ in candidates:
        try:
            data = generate(model, name, args.prompt, key, args.timeout)
            used = name
            break
        except urllib.error.HTTPError as exc:
            failures.append(f"{name}: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:160]}")
        except Exception as exc:  # noqa: BLE001 - collected and reported below
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    if not data:
        die(
            "every provider refused the request — most often a token without the "
            "'Inference Providers' permission (run --check):\n  " + "\n  ".join(failures)
        )

    suffix = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
    out_path = Path(args.out) if args.out else Path("output") / f"hf-image.{suffix}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    if out_path.stat().st_size == 0:
        die(f"wrote an empty file: {out_path}")

    print(
        json.dumps(
            {
                "path": str(out_path),
                "bytes": out_path.stat().st_size,
                "model": model,
                "provider": used,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
