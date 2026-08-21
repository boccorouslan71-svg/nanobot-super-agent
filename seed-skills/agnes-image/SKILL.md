---
name: agnes-image
description: Generate or edit images with Agnes AI (agnes-image-2.1-flash). Use when the user wants an image created from a text prompt, an existing image restyled or transformed (image-to-image), or several reference images composed into one. Free at the time of writing and the most reliable of the installed image tools.
---

# Agnes AI image generation

`agnes-image-2.1-flash` — text-to-image, image-to-image, and multi-image
composition. Returns a hosted URL by default, or base64.

## Credentials

Read from the environment, in this order:

1. `AGNES_IMAGE_API_KEY`
2. `AGNES_API_KEY` (the same key the chat provider uses — already set on Render)

No key in the environment is a hard error, never a silent skip.

## Usage

```bash
# text to image
python3 skills/agnes-image/scripts/gen_image.py --prompt "a red fox in a snowy forest, cinematic" --size 1K --ratio 16:9

# image to image (restyle, keep composition)
python3 skills/agnes-image/scripts/gen_image.py --prompt "make it a rain-soaked cyberpunk night" --image https://example.com/in.png

# compose several references into one image
python3 skills/agnes-image/scripts/gen_image.py --prompt "combine both characters into one battle scene" --image URL_A --image URL_B

# download the result next to the agent's other outputs
python3 skills/agnes-image/scripts/gen_image.py --prompt "..." --out output/fox.png
```

The script prints one JSON line: `{"url": ..., "path": ..., "size": ..., "ratio": ...}`.
Send the URL straight to the user, or `--out` first when a local file is needed.

## Size and ratio

Pass `--size` as a tier (`1K`, `2K`, `3K`, `4K`) together with `--ratio`
(`1:1`, `3:4`, `4:3`, `16:9`, `9:16`, `2:3`, `3:2`, `21:9`). Exact pixel sizes
like `1920x1080` are accepted but get mapped to the nearest tier, so for a
16:9 asset ask for `--size 2K --ratio 16:9` and crop afterwards if the exact
canvas matters.

## Notes

- Endpoint: `POST https://apihub.agnes-ai.com/v1/images/generations`.
- `--ratio` and image inputs travel inside `extra_body`, which is what the API expects.
- Generation takes ~10-40s; the script waits up to `--timeout` (default 180s).
- If the API answers with an error payload, the script exits non-zero and prints
  the provider's message — do not retry blindly, read it first.
