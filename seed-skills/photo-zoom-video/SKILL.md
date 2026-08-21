---
name: photo-zoom-video
description: "Turn a photo into a video with a slow zoom / Ken Burns move (vertical Reels-TikTok-Shorts, horizontal YouTube, square, 4K), optionally with music, fades, and several photos concatenated into one clip. Use whenever the user asks for a zoom video from a picture — including in French: « crée une vidéo zoom de cette photo », « fais un zoom sur cette image », « transforme cette photo en vidéo », « effet Ken Burns »."
metadata: {"nanobot":{"emoji":"🎞️","requires":{"bins":["ffmpeg","ffprobe"]},"install":[{"id":"apt","kind":"apt","package":"ffmpeg","bins":["ffmpeg","ffprobe"],"label":"Install ffmpeg (apt)"},{"id":"brew","kind":"brew","formula":"ffmpeg","bins":["ffmpeg","ffprobe"],"label":"Install ffmpeg (brew)"}]}}
---

# Photo → zoom video (Ken Burns)

Renders a still photo as a moving clip with `ffmpeg`'s `zoompan`. One script does
everything; it validates its own output, so a clip it reports is a clip that
plays.

## Usage

```bash
# the default: 8s vertical Reels/TikTok clip, slow zoom in to 135%
python3 skills/photo-zoom-video/scripts/zoom_video.py <photo> --outdir output

# a photo the user just sent (Telegram attachments are read-only, so write to output/)
python3 skills/photo-zoom-video/scripts/zoom_video.py ~/.nanobot/media/telegram/<file>.jpg --outdir output

# pull back instead of pushing in
python3 skills/photo-zoom-video/scripts/zoom_video.py photo.jpg --effect out

# zoom in on a face that sits left of centre and above the middle
python3 skills/photo-zoom-video/scripts/zoom_video.py photo.jpg --focus 0.35,0.30

# a real Ken Burns move: zoom plus a lateral travel
python3 skills/photo-zoom-video/scripts/zoom_video.py photo.jpg --effect kenburns-lr

# horizontal YouTube, 12s, gentle 1.2x zoom, with music
python3 skills/photo-zoom-video/scripts/zoom_video.py photo.jpg --preset youtube --duration 12 --zoom 1.2 --music song.mp3

# several photos -> one video (each gets its own clip, then they are concatenated)
python3 skills/photo-zoom-video/scripts/zoom_video.py a.jpg b.jpg c.jpg --duration 6 --outdir output
```

The script prints one JSON line: `{"path": ..., "size": "1080x1920", "duration_s": ..., "clips": [...]}`.

## Delivering the result

The clip is a local file, not a URL. Send it with the `message` tool, passing the
JSON `path` in `media` — that is what puts the video in the chat. Do not paste
the path as text and do not use `read_file` on an mp4.

Write outputs under `output/` in the workspace. Inbound attachments
(`~/.nanobot/media/...`) are readable but not writable, and generated videos are
deliberately not mirrored to durable storage — regenerate rather than archive.

## Options that matter

| Flag | Default | Notes |
|---|---|---|
| `--duration` | `8` | seconds per photo |
| `--zoom` | `1.35` | final zoom; `1.2` is subtle, `1.6` is aggressive |
| `--effect` | `in` | `in`, `out`, `kenburns-lr`, `kenburns-rl`, `kenburns-tb`, `kenburns-bt` |
| `--focus` | `0.5,0.5` | zoom centre as fractions of the image |
| `--preset` | `reels` | `reels`/`tiktok`/`shorts` 1080x1920@30, `youtube` 1920x1080@25, `square` 1080x1080@30, `vertical4k` 2160x3840@30 |
| `--size` | — | explicit `WxH` (even numbers only), overrides the preset |
| `--fit` | `crop` | `crop` fills the frame; `pad` letterboxes instead of cropping |
| `--music` | — | audio file or URL; audio is faded and cut to the video length |
| `--fade` | `0.5` | fade in/out seconds; `0` disables |
| `--crf` / `--x264-preset` | `23` / `medium` | lower crf = better quality, bigger file; `--x264-preset slow` shrinks it |
| `--no-concat` | off | keep batch clips separate instead of joining them |

## How it works, and why each part is there

1. **Fit then pre-scale.** The source is scaled up (long side `--prescale`, default
   4000) *and* fitted to the output aspect ratio before `zoompan`. The upscale is
   what keeps the zoom smooth — without it the motion stutters. The aspect fit is
   what stops a landscape photo from being squashed into a 9:16 frame, which
   `zoompan` alone would do.
2. **Increment from the duration.** `increment = (zoom − 1) / (fps × duration)`,
   so the zoom finishes exactly at the end of the clip: 1.0 → 1.4 over 10s at
   25fps gives `0.4 / 250 = 0.0016` per frame.
3. **`d` covers the whole clip.** `d` is set to `fps × duration` frames; a smaller
   value makes the zoom restart mid-video.
4. **`yuv420p` twice** (filter and `-pix_fmt`), plus `+faststart`, so phones and
   social platforms can actually play the file.
5. **Then it is probed.** Every render is checked with `ffprobe` for real
   dimensions, duration, and (with `--music`) an audio stream. A mismatch is an
   error, never a silent pass.

## Failure modes

- **`missing required binary: ffmpeg`** — the host has no ffmpeg. On Debian/Ubuntu:
  `apt-get install -y ffmpeg`. The skill is listed as unavailable until then.
- **An ffmpeg error** is printed with its last lines and a non-zero exit. Read it;
  do not retry the same command blindly.
- **Odd dimensions** are rejected up front — h264 needs even width and height.
- **`--music` with several photos** is refused: render the batch, then add audio
  to the concatenated file in a second pass.
