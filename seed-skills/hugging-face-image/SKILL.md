---
name: hugging-face-image
description: Generate images through Hugging Face Inference Providers (Z-Image-Turbo, FLUX.1, Stable Diffusion). Use only when Agnes AI and Cloudflare are both unsuitable — this route needs a token with the "Inference Providers" permission and consumes HF credits, so it is the third choice, not the default.
---

# Hugging Face image generation

Routes to whichever inference provider currently serves the model (fal-ai,
nscale, wavespeed, replicate, …). Hugging Face's own `hf-inference` backend no
longer serves these image models, so the provider mapping is resolved from the
model's metadata at run time rather than hardcoded.

## Prerequisite (this skill fails without it)

`HF_API_KEY` must be a token with the **Inference Providers** permission
enabled. A read-only or fine-grained token without it gets
`Model not supported by provider …` for every provider, even ones the model
metadata reports as live. Check with:

```bash
python3 skills/hugging-face-image/scripts/gen_image.py --check
```

`--check` prints the token identity and, per provider, whether a real
generation call is accepted. Fix the token in Hugging Face settings before
using this skill in a workflow.

## Usage

```bash
# default model (Z-Image-Turbo), automatic provider
python3 skills/hugging-face-image/scripts/gen_image.py --prompt "a red fox in a snowy forest" --out output/fox.png

# a specific model, a specific provider
python3 skills/hugging-face-image/scripts/gen_image.py --model black-forest-labs/FLUX.1-schnell --provider nscale --prompt "..."
```

Prints one JSON line: `{"path": ..., "bytes": ..., "model": ..., "provider": ...}`.

## Notes

- Model aliases: `z-image-turbo` (default), `flux-schnell`, `flux-dev`,
  `sd-3.5-large`, `sdxl-base`. Any full `owner/model` id also works.
- Free HF credits are small; a paid provider balance is usually needed for
  volume. Agnes AI (`agnes-image`) and Cloudflare (`cloudflare-ai-image`) are
  the free workhorses.
