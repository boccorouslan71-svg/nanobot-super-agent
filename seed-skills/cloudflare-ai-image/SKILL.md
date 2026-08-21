---
name: cloudflare-ai-image
description: Generate images with Cloudflare Workers AI (FLUX.1-schnell, SDXL Lightning, SDXL Base, Dreamshaper-8). Use as the fast free fallback when Agnes AI is unavailable, or when a specific Stable Diffusion look is wanted. Returns a local PNG file rather than a hosted URL.
---

# Cloudflare Workers AI image generation

Four text-to-image models on Cloudflare's free Workers AI allowance. Output is
written to disk (Cloudflare returns image bytes, not a hosted link).

## Credentials

From the environment: `CF_API_TOKEN` and `CF_ACCOUNT_ID` (both set on Render).
Falls back to `scripts/config.json` with the same two keys. Missing either one
is a hard error.

## Usage

```bash
# default model (flux-schnell), writes output/<slug>.png
python3 skills/cloudflare-ai-image/scripts/gen_image.py --prompt "a red fox in a snowy forest"

# pick a model and a destination
python3 skills/cloudflare-ai-image/scripts/gen_image.py --model sdxl-lightning --prompt "..." --out output/poster.png

# list the available models
python3 skills/cloudflare-ai-image/scripts/gen_image.py --list-models
```

Prints one JSON line: `{"path": ..., "bytes": ..., "model": ...}`.

## Models

| `--model` | Cloudflare model | Notes |
|---|---|---|
| `flux-schnell` (default) | `@cf/black-forest-labs/flux-1-schnell` | Best quality/speed; 4-8 steps |
| `sdxl-lightning` | `@cf/bytedance/stable-diffusion-xl-lightning` | Fastest |
| `sdxl-base` | `@cf/stabilityai/stable-diffusion-xl-base-1.0` | Supports negative prompts |
| `dreamshaper-8` | `@cf/lykon/dreamshaper-8-lcm` | Stylized / illustrative |

## Notes

- FLUX returns base64 JSON; the SD models return raw PNG bytes. The script
  handles both and verifies the file is non-empty before reporting success.
- `--steps` applies to FLUX (1-8, default 4). `--negative` applies to the SD models.
- No aspect-ratio parameter: use `--width` / `--height` on the SD models, or
  generate square and crop.
