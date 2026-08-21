#!/usr/bin/env python3
"""Turn a photo into a zooming (Ken Burns) video with ffmpeg.

Implements the guide's recipe — a big pre-scale before ``zoompan``, an
increment derived from ``(zoom_final - 1) / (fps * duration)``, ``d`` pinned to
the full frame count, and ``yuv420p`` on the way out — and adds the two things a
caller cannot verify by eye: the source is fitted to the target aspect ratio
before zooming (``zoompan`` alone squashes a landscape photo into a 9:16 frame),
and every render is probed afterwards.

Fails loudly. A missing binary, an unreadable photo, an ffmpeg error, or an
output whose duration or dimensions do not match the request all exit non-zero
with the reason. A silent, squashed, or zero-length video is indistinguishable
from success to the agent delivering it, so none of those may pass.

Prints one JSON line describing the result.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# name -> (width, height, fps) — the guide's format cheat-sheet.
PRESETS: dict[str, tuple[int, int, int]] = {
    "reels": (1080, 1920, 30),
    "tiktok": (1080, 1920, 30),
    "shorts": (1080, 1920, 30),
    "youtube": (1920, 1080, 25),
    "square": (1080, 1080, 30),
    "vertical4k": (2160, 3840, 30),
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
EFFECTS = ("in", "out", "kenburns-lr", "kenburns-rl", "kenburns-tb", "kenburns-bt")


def die(message: str) -> None:
    print(f"photo-zoom-video: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_binaries() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        die(
            f"missing required binary/binaries: {', '.join(missing)}. "
            "Install ffmpeg (apt-get install -y ffmpeg) — this skill cannot render without it."
        )


@dataclass
class Spec:
    """A validated render request. Built once, then only read."""

    width: int
    height: int
    fps: int
    duration: float
    zoom: float
    effect: str
    focus_x: float
    focus_y: float
    prescale: int
    fit: str
    fade: float
    crf: int
    x264_preset: str
    music: Path | None = None

    frames: int = field(init=False)
    increment: float = field(init=False)

    def __post_init__(self) -> None:
        # d must cover the whole clip: a smaller value restarts the zoom
        # mid-video, which is the guide's second classic pitfall.
        self.frames = max(1, math.ceil(self.fps * self.duration))
        # The guide's rule: increment = (zoom_final - 1) / (fps x duration).
        self.increment = round((self.zoom - 1.0) / self.frames, 8)

    @property
    def aspect(self) -> float:
        return self.width / self.height


def parse_focus(raw: str) -> tuple[float, float]:
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    if len(parts) != 2:
        die(f"--focus must be 'x,y' as fractions of the image, e.g. 0.35,0.30 (got {raw!r})")
    try:
        x, y = float(parts[0]), float(parts[1])
    except ValueError:
        die(f"--focus values must be numbers, e.g. 0.35,0.30 (got {raw!r})")
        raise AssertionError("unreachable")
    for name, value in (("x", x), ("y", y)):
        if not 0.0 <= value <= 1.0:
            die(f"--focus {name} must be between 0 and 1 (got {value})")
    return x, y


def resolve_input(raw: str, scratch: Path) -> Path:
    """Return a local, existing image path. Downloads http(s) sources."""
    if raw.startswith(("http://", "https://")):
        target = scratch / f"input_{abs(hash(raw)) % 10**8}"
        try:
            with urllib.request.urlopen(raw, timeout=60) as response:  # noqa: S310 - explicit user input
                payload = response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            die(f"could not download {raw}: {exc}")
            raise AssertionError("unreachable")
        if not payload:
            die(f"{raw} returned an empty body")
        target.write_bytes(payload)
        return target
    path = Path(raw).expanduser()
    if not path.is_file():
        die(f"photo not found: {path}")
    if path.stat().st_size == 0:
        die(f"photo is empty: {path}")
    if path.suffix and path.suffix.lower() not in IMAGE_SUFFIXES:
        die(f"{path.name} does not look like an image (suffix {path.suffix!r})")
    return path


def build_filter(spec: Spec) -> str:
    """Compose the filter chain: fit -> pre-scale -> zoompan -> fades -> format."""
    # Pre-scale target, output aspect preserved, long side ~= --prescale. This is
    # the guide's anti-jitter step; keeping the aspect is what stops zoompan from
    # distorting a photo whose shape differs from the output frame.
    if spec.aspect >= 1.0:
        pre_w = spec.prescale
        pre_h = max(2, int(round(spec.prescale / spec.aspect)))
    else:
        pre_h = spec.prescale
        pre_w = max(2, int(round(spec.prescale * spec.aspect)))
    pre_w -= pre_w % 2
    pre_h -= pre_h % 2

    if spec.fit == "pad":
        fit = (
            f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=decrease,"
            f"pad={pre_w}:{pre_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        fit = (
            f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase,"
            f"crop={pre_w}:{pre_h}"
        )

    inc = f"{spec.increment:.8f}".rstrip("0")
    zoom_max = f"{spec.zoom:g}"
    frames = spec.frames

    if spec.effect == "out":
        # Start wide-of-frame and walk back: the guide's dezoom expression.
        z = f"if(lte(zoom,1.0),{zoom_max},max(1.001,zoom-{inc}))"
    else:
        z = f"min(zoom+{inc},{zoom_max})"

    x = f"iw*{spec.focus_x:g}-(iw/zoom/2)"
    y = f"ih*{spec.focus_y:g}-(ih/zoom/2)"
    # A true Ken Burns move: zoom plus a lateral travel driven by on/frames,
    # which runs 0 -> 1 across the clip.
    if spec.effect == "kenburns-lr":
        x = f"(iw-iw/zoom)*(on/{frames})"
    elif spec.effect == "kenburns-rl":
        x = f"(iw-iw/zoom)*(1-on/{frames})"
    elif spec.effect == "kenburns-tb":
        y = f"(ih-ih/zoom)*(on/{frames})"
    elif spec.effect == "kenburns-bt":
        y = f"(ih-ih/zoom)*(1-on/{frames})"

    chain = [
        fit,
        (
            f"zoompan=z='{z}':d={frames}:x='{x}':y='{y}'"
            f":fps={spec.fps}:s={spec.width}x{spec.height}"
        ),
    ]
    if spec.fade > 0 and spec.duration > 2 * spec.fade:
        chain.append(f"fade=t=in:d={spec.fade:g}")
        chain.append(f"fade=t=out:st={spec.duration - spec.fade:g}:d={spec.fade:g}")
    chain.append("format=yuv420p")
    return ",".join(chain)


def run(cmd: list[str], *, what: str) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        die(f"{what} failed (exit {proc.returncode}):\n{tail}")
    return proc.stdout


def probe(path: Path) -> dict[str, object]:
    raw = run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        what=f"ffprobe of {path.name}",
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"ffprobe returned invalid JSON for {path.name}: {exc}")
        raise AssertionError("unreachable")
    if not isinstance(data, dict):
        die(f"ffprobe returned an unexpected shape for {path.name}")
    return data


def verify_output(path: Path, spec: Spec, expected_duration: float, *, want_audio: bool) -> dict[str, object]:
    """Confirm the render is real: probe it instead of trusting exit code 0."""
    if not path.is_file() or path.stat().st_size == 0:
        die(f"ffmpeg reported success but produced no usable file at {path}")
    data = probe(path)
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        die(f"{path.name} contains no streams")
    video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    if video is None:
        die(f"{path.name} has no video stream")
    width, height = video.get("width"), video.get("height")
    if (width, height) != (spec.width, spec.height):
        die(f"{path.name} is {width}x{height}, expected {spec.width}x{spec.height}")
    fmt = data.get("format")
    duration = float(fmt.get("duration", 0.0)) if isinstance(fmt, dict) else 0.0
    if abs(duration - expected_duration) > max(0.75, expected_duration * 0.12):
        die(f"{path.name} lasts {duration:.2f}s, expected ~{expected_duration:.2f}s")
    audio = any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)
    if want_audio and not audio:
        die(f"{path.name} was rendered with music but carries no audio stream")
    return {
        "duration_s": round(duration, 2),
        "width": spec.width,
        "height": spec.height,
        "bytes": path.stat().st_size,
        "has_audio": audio,
    }


def render_clip(source: Path, target: Path, spec: Spec, *, with_music: bool) -> dict[str, object]:
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(source)]
    if with_music and spec.music is not None:
        cmd += ["-i", str(spec.music)]
    vf = build_filter(spec)
    cmd += ["-vf", vf, "-t", f"{spec.duration:g}"]
    cmd += [
        "-c:v", "libx264",
        "-preset", spec.x264_preset,
        "-crf", str(spec.crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-r", str(spec.fps),
    ]
    if with_music and spec.music is not None:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-shortest"]
        if spec.fade > 0 and spec.duration > 2 * spec.fade:
            cmd += [
                "-af",
                f"afade=t=in:d={spec.fade:g},afade=t=out:st={spec.duration - spec.fade:g}:d={spec.fade:g}",
            ]
    else:
        cmd += ["-an"]
    cmd.append(str(target))
    run(cmd, what=f"ffmpeg render of {source.name}")
    return verify_output(target, spec, spec.duration, want_audio=with_music and spec.music is not None)


def concat(clips: list[Path], target: Path, spec: Spec, scratch: Path) -> dict[str, object]:
    listing = scratch / "concat.txt"
    listing.write_text("".join(f"file '{clip.resolve()}'\n" for clip in clips))
    run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(target)],
        what="ffmpeg concat",
    )
    return verify_output(
        target, spec, spec.duration * len(clips), want_audio=False
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Photo -> zooming video (Ken Burns) via ffmpeg",
        epilog="Outputs one JSON line: {\"path\": ..., \"clips\": [...], ...}",
    )
    parser.add_argument("images", nargs="+", help="photo path(s) or http(s) URL(s)")
    parser.add_argument("--out", help="output .mp4 path (single photo, or the concatenated batch)")
    parser.add_argument("--outdir", default="output", help="directory for outputs (default: output)")
    parser.add_argument("--duration", type=float, default=8.0, help="seconds per photo (default 8)")
    parser.add_argument("--fps", type=int, help="frames per second (default: from --preset)")
    parser.add_argument("--zoom", type=float, default=1.35, help="final zoom, e.g. 1.35 = 135%%")
    parser.add_argument("--effect", default="in", choices=EFFECTS, help="default: in")
    parser.add_argument(
        "--focus",
        default="0.5,0.5",
        help="zoom centre as fractions, e.g. 0.35,0.30 for a face left of centre",
    )
    parser.add_argument("--preset", default="reels", choices=sorted(PRESETS), help="default: reels")
    parser.add_argument("--size", help="explicit WxH, overrides --preset (e.g. 1080x1920)")
    parser.add_argument("--fit", default="crop", choices=("crop", "pad"), help="default: crop")
    parser.add_argument("--music", help="audio path or URL (single photo only)")
    parser.add_argument("--fade", type=float, default=0.5, help="fade in/out seconds, 0 to disable")
    parser.add_argument("--crf", type=int, default=23, help="quality/size knob (default 23)")
    parser.add_argument("--x264-preset", default="medium", dest="x264_preset")
    parser.add_argument("--prescale", type=int, default=4000, help="pre-zoom long side (default 4000)")
    parser.add_argument("--no-concat", action="store_true", help="keep batch clips separate")
    args = parser.parse_args()

    require_binaries()

    if args.duration <= 0:
        die("--duration must be positive")
    if args.zoom <= 1.0:
        die("--zoom must be greater than 1.0 (1.35 means zoom to 135%)")
    if args.prescale < 640:
        die("--prescale below 640 defeats its purpose (it exists to keep the zoom smooth)")

    width, height, preset_fps = PRESETS[args.preset]
    if args.size:
        try:
            raw_w, raw_h = (int(v) for v in args.size.lower().split("x", 1))
        except ValueError:
            die(f"--size must look like 1080x1920 (got {args.size!r})")
            raise AssertionError("unreachable")
        if raw_w % 2 or raw_h % 2:
            die(f"--size must use even dimensions, h264 cannot encode {raw_w}x{raw_h}")
        width, height = raw_w, raw_h
    fps = args.fps or preset_fps
    if fps <= 0:
        die("--fps must be positive")

    focus_x, focus_y = parse_focus(args.focus)

    with tempfile.TemporaryDirectory(prefix="zoomvid-") as tmp:
        scratch = Path(tmp)

        music: Path | None = None
        if args.music:
            if len(args.images) > 1 and not args.no_concat:
                die("--music applies to a single clip; render the batch first, then add audio")
            music = resolve_input(args.music, scratch) if args.music.startswith(
                ("http://", "https://")
            ) else Path(args.music).expanduser()
            if not music.is_file():
                die(f"music file not found: {music}")

        spec = Spec(
            width=width,
            height=height,
            fps=fps,
            duration=args.duration,
            zoom=args.zoom,
            effect=args.effect,
            focus_x=focus_x,
            focus_y=focus_y,
            prescale=args.prescale,
            fit=args.fit,
            fade=max(0.0, args.fade),
            crf=args.crf,
            x264_preset=args.x264_preset,
            music=music,
        )

        sources = [resolve_input(raw, scratch) for raw in args.images]
        outdir = Path(args.outdir).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)

        clips: list[Path] = []
        details: list[dict[str, object]] = []
        single = len(sources) == 1
        for index, source in enumerate(sources, start=1):
            if single and args.out:
                target = Path(args.out).expanduser()
            else:
                stem = source.stem or f"photo{index}"
                suffix = "" if single else f"_{index:02d}"
                target = outdir / f"{stem}{suffix}_zoom.mp4"
            info = render_clip(source, target, spec, with_music=music is not None)
            clips.append(target)
            details.append({"path": str(target), **info})

        result: dict[str, object] = {
            "effect": spec.effect,
            "zoom": spec.zoom,
            "increment_per_frame": spec.increment,
            "frames": spec.frames,
            "fps": spec.fps,
            "size": f"{spec.width}x{spec.height}",
            "duration_s": spec.duration,
            "clips": details,
        }

        if len(clips) > 1 and not args.no_concat:
            final = Path(args.out).expanduser() if args.out else outdir / "zoom_final.mp4"
            info = concat(clips, final, spec, scratch)
            result["path"] = str(final)
            result["concatenated"] = len(clips)
            result.update({k: v for k, v in info.items() if k in ("duration_s", "bytes")})
        else:
            result["path"] = str(clips[0])
            result["bytes"] = details[0]["bytes"]

        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
