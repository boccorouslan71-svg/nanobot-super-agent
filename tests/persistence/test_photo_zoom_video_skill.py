"""The photo-zoom-video skill: its maths, its guard rails, and one real render.

The skill is shipped in the image and seeded into the data dir, so a regression
here reaches the running agent silently — the failure the user sees is a squashed
or stuttering clip, not a stack trace. These tests pin the three things that are
invisible from the output path alone: the zoom increment lands exactly on the
requested final zoom, the source is fitted to the target aspect before zooming,
and bad input is refused up front instead of producing a broken video.

The render test is skipped when ffmpeg is absent so the suite still runs on a
machine without it; the Dockerfile check in the deployment validation is what
guarantees the deployed image *has* it.
"""

from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "seed-skills" / "photo-zoom-video"
SCRIPT = SKILL_DIR / "scripts" / "zoom_video.py"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _load_module():
    spec = importlib.util.spec_from_file_location("zoom_video", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the script's @dataclass resolves its annotations
    # through sys.modules, and a module absent from it fails at class creation.
    sys.modules.setdefault("zoom_video", module)
    spec.loader.exec_module(module)
    return module


zoom_video = _load_module()


def _spec(**overrides):
    defaults = dict(
        width=1080,
        height=1920,
        fps=25,
        duration=10.0,
        zoom=1.4,
        effect="in",
        focus_x=0.5,
        focus_y=0.5,
        prescale=4000,
        fit="crop",
        fade=0.0,
        crf=23,
        x264_preset="medium",
    )
    defaults.update(overrides)
    return zoom_video.Spec(**defaults)


def _photo(path: Path, width: int, height: int) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (18, 28, 58))
    draw = ImageDraw.Draw(image)
    step = max(20, width // 16)
    for x in range(0, width, step):
        draw.line([(x, 0), (x, height)], fill=(210, 210, 90), width=3)
    for y in range(0, height, step):
        draw.line([(0, y), (width, y)], fill=(210, 210, 90), width=3)
    # A round marker, square-bounded in SOURCE pixels so it is a true circle:
    # measuring its width across frames is how the render test proves the zoom
    # moved, and its roundness proves nothing was squashed.
    side = min(width, height) * 0.20
    cx, cy = width / 2, height / 2
    draw.ellipse((cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2), fill=(235, 60, 60))
    image.save(path, quality=92)
    return path


class TestZoomMaths:
    def test_increment_matches_the_documented_rule(self) -> None:
        """increment = (zoom_final - 1) / (fps x duration); 1.0->1.4 in 10s @25 = 0.0016."""
        spec = _spec(zoom=1.4, fps=25, duration=10.0)

        assert spec.frames == 250
        assert spec.increment == pytest.approx(0.0016)

    def test_the_zoom_lands_on_the_requested_value(self) -> None:
        for zoom, fps, duration in ((1.35, 30, 8.0), (1.2, 25, 12.0), (1.6, 30, 5.0)):
            spec = _spec(zoom=zoom, fps=fps, duration=duration)
            reached = 1.0 + spec.increment * spec.frames
            assert reached == pytest.approx(zoom, abs=0.01), f"{zoom} @ {fps}fps/{duration}s"

    def test_d_covers_the_whole_clip(self) -> None:
        """A d below fps x duration restarts the zoom mid-video."""
        spec = _spec(fps=30, duration=7.5)

        assert spec.frames >= math.ceil(30 * 7.5)
        assert f"d={spec.frames}" in zoom_video.build_filter(spec)

    def test_a_fractional_duration_still_covers_every_frame(self) -> None:
        spec = _spec(fps=30, duration=4.1)

        assert spec.frames == 123


class TestFilterChain:
    def test_scale_precedes_zoompan(self) -> None:
        """The pre-scale is the anti-stutter step; after zoompan it does nothing."""
        chain = zoom_video.build_filter(_spec())

        assert chain.index("scale=") < chain.index("zoompan=")

    def test_the_prescale_keeps_the_output_aspect(self) -> None:
        """Feeding zoompan a differently-shaped frame is what squashes the photo."""
        chain = zoom_video.build_filter(_spec(width=1080, height=1920))

        assert "crop=2250:4000" in chain
        assert "s=1080x1920" in chain

    def test_horizontal_output_prescales_on_the_long_side(self) -> None:
        chain = zoom_video.build_filter(_spec(width=1920, height=1080))

        assert "crop=4000:2250" in chain

    def test_pad_fit_letterboxes_instead_of_cropping(self) -> None:
        chain = zoom_video.build_filter(_spec(fit="pad"))

        assert "force_original_aspect_ratio=decrease" in chain
        assert "pad=" in chain
        assert "crop=" not in chain

    def test_yuv420p_is_always_last(self) -> None:
        """Without it the file will not play on phones or social platforms."""
        assert zoom_video.build_filter(_spec()).endswith("format=yuv420p")

    def test_zoom_out_walks_back_from_the_max(self) -> None:
        chain = zoom_video.build_filter(_spec(effect="out"))

        assert "if(lte(zoom,1.0),1.4," in chain
        assert "max(1.001,zoom-" in chain

    def test_kenburns_travels_across_the_frame(self) -> None:
        spec = _spec(effect="kenburns-lr")

        chain = zoom_video.build_filter(spec)

        assert f"(iw-iw/zoom)*(on/{spec.frames})" in chain
        assert "min(zoom+" in chain, "a Ken Burns move zooms as well as travels"

    def test_reverse_kenburns_starts_at_the_far_side(self) -> None:
        spec = _spec(effect="kenburns-rl")

        assert f"(iw-iw/zoom)*(1-on/{spec.frames})" in zoom_video.build_filter(spec)

    def test_focus_moves_the_zoom_centre(self) -> None:
        chain = zoom_video.build_filter(_spec(focus_x=0.35, focus_y=0.3))

        assert "iw*0.35-(iw/zoom/2)" in chain
        assert "ih*0.3-(ih/zoom/2)" in chain

    def test_fades_are_placed_at_both_ends(self) -> None:
        chain = zoom_video.build_filter(_spec(duration=10.0, fade=0.5))

        assert "fade=t=in:d=0.5" in chain
        assert "fade=t=out:st=9.5:d=0.5" in chain

    def test_a_fade_longer_than_the_clip_is_dropped(self) -> None:
        """A 2s fade on a 1s clip would black out the whole video."""
        assert "fade=t=in" not in zoom_video.build_filter(_spec(duration=1.0, fade=2.0))


class TestBadInputIsRefused:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
        )

    def test_a_missing_photo_exits_non_zero(self, tmp_path: Path) -> None:
        result = self._run(str(tmp_path / "absent.jpg"))

        assert result.returncode != 0
        assert "photo not found" in result.stderr

    def test_an_empty_photo_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jpg"
        empty.touch()

        result = self._run(str(empty))

        assert result.returncode != 0
        assert "empty" in result.stderr

    def test_odd_dimensions_are_refused_before_encoding(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 400, 300)

        result = self._run(str(photo), "--size", "1081x1920")

        assert result.returncode != 0
        assert "even dimensions" in result.stderr

    def test_a_zoom_of_one_is_refused(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 400, 300)

        result = self._run(str(photo), "--zoom", "1.0")

        assert result.returncode != 0
        assert "greater than 1.0" in result.stderr

    def test_focus_outside_the_image_is_refused(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 400, 300)

        result = self._run(str(photo), "--focus", "1.4,0.2")

        assert result.returncode != 0
        assert "between 0 and 1" in result.stderr

    def test_music_with_a_batch_is_refused(self, tmp_path: Path) -> None:
        a = _photo(tmp_path / "a.jpg", 400, 300)
        b = _photo(tmp_path / "b.jpg", 400, 300)
        song = tmp_path / "song.mp3"
        song.write_bytes(b"\x00" * 64)

        result = self._run(str(a), str(b), "--music", str(song))

        assert result.returncode != 0
        assert "single clip" in result.stderr

    def test_a_missing_ffmpeg_names_the_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(zoom_video.shutil, "which", lambda _name: None)

        with pytest.raises(SystemExit) as excinfo:
            zoom_video.require_binaries()

        assert excinfo.value.code == 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed on this host")
class TestRealRender:
    def _measure_marker(self, video: Path, frame: int, tmp_path: Path) -> tuple[int, int]:
        """Return (marker width, marker centre x) at a given frame."""
        import numpy as np
        from PIL import Image

        still = tmp_path / f"frame_{frame}.png"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-i", str(video),
                "-vf", f"select=eq(n\\,{frame})", "-vsync", "0", "-frames:v", "1", str(still),
            ],
            check=True,
        )
        pixels = np.array(Image.open(still).convert("RGB")).astype(int)
        mask = (pixels[:, :, 0] > 150) & (pixels[:, :, 1] < 110) & (pixels[:, :, 2] < 110)
        assert mask.sum() > 50, f"marker not visible at frame {frame}"
        columns = np.nonzero(mask)[1]
        return int(columns.max() - columns.min()), int((columns.max() + columns.min()) // 2)

    def _render(self, photo: Path, out: Path, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(photo), "--out", str(out), *args],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_a_landscape_photo_fills_a_vertical_frame_without_squashing(
        self, tmp_path: Path
    ) -> None:
        photo = _photo(tmp_path / "landscape.jpg", 1600, 1200)  # 4:3 into 9:16
        out = tmp_path / "clip.mp4"

        payload = self._render(photo, out, "--duration", "2", "--fade", "0", "--prescale", "1600")

        assert payload["size"] == "1080x1920"
        assert out.is_file() and out.stat().st_size > 0
        # The marker is a circle: if zoompan had stretched the source to fit, it
        # would render as an ellipse.
        import numpy as np
        from PIL import Image

        still = tmp_path / "still.png"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-i", str(out),
                "-vf", "select=eq(n\\,1)", "-vsync", "0", "-frames:v", "1", str(still),
            ],
            check=True,
        )
        pixels = np.array(Image.open(still).convert("RGB")).astype(int)
        mask = (pixels[:, :, 0] > 150) & (pixels[:, :, 1] < 110) & (pixels[:, :, 2] < 110)
        rows, columns = np.nonzero(mask)
        width = columns.max() - columns.min()
        height = rows.max() - rows.min()
        assert abs(width - height) <= max(6, width * 0.06), (
            f"marker is {width}x{height}: the source was distorted to fit the frame"
        )

    def test_the_zoom_progresses_and_never_restarts(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 1200, 1200)
        out = tmp_path / "zoomin.mp4"

        self._render(
            photo, out, "--duration", "4", "--fps", "25", "--zoom", "1.4",
            "--fade", "0", "--prescale", "1600",
        )

        widths = [self._measure_marker(out, frame, tmp_path)[0] for frame in (1, 40, 75, 97)]
        assert all(b >= a - 2 for a, b in zip(widths, widths[1:])), (
            f"zoom is not monotonic, it restarted mid-clip: {widths}"
        )
        assert widths[-1] / widths[0] == pytest.approx(1.4, abs=0.12), widths

    def test_zoom_out_shrinks_the_subject(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 1200, 1200)
        out = tmp_path / "zoomout.mp4"

        self._render(
            photo, out, "--effect", "out", "--duration", "3", "--fps", "25",
            "--fade", "0", "--prescale", "1600",
        )

        first, _ = self._measure_marker(out, 1, tmp_path)
        last, _ = self._measure_marker(out, 70, tmp_path)
        assert last < first, f"dezoom did not pull back: {first} -> {last}"

    def test_kenburns_travels_sideways_while_zooming(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 1600, 1200)
        out = tmp_path / "kb.mp4"

        self._render(
            photo, out, "--effect", "kenburns-lr", "--duration", "3", "--fps", "25",
            "--fade", "0", "--prescale", "1600",
        )

        width_first, centre_first = self._measure_marker(out, 1, tmp_path)
        width_last, centre_last = self._measure_marker(out, 70, tmp_path)
        assert width_last > width_first, "the clip did not zoom"
        assert abs(centre_last - centre_first) > 20, "the clip did not travel"

    def test_music_produces_an_audio_stream(self, tmp_path: Path) -> None:
        photo = _photo(tmp_path / "p.jpg", 800, 800)
        song = tmp_path / "tone.mp3"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=6", str(song),
            ],
            check=True,
        )
        out = tmp_path / "with_music.mp4"

        payload = self._render(
            photo, out, "--duration", "2", "--music", str(song), "--prescale", "1200"
        )

        assert payload["clips"][0]["has_audio"] is True

    def test_a_batch_is_concatenated_into_one_clip(self, tmp_path: Path) -> None:
        photos = [_photo(tmp_path / f"p{i}.jpg", 800, 800) for i in range(3)]
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), *map(str, photos),
                "--duration", "2", "--fade", "0", "--prescale", "1200",
                "--outdir", str(tmp_path / "out"),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["concatenated"] == 3
        assert payload["duration_s"] == pytest.approx(6.0, abs=0.6)
        assert Path(payload["path"]).is_file()

    def test_a_corrupt_render_is_reported_not_returned(self, tmp_path: Path) -> None:
        """verify_output is the last line of defence: exit 0 is not proof."""
        empty = tmp_path / "broken.mp4"
        empty.touch()

        with pytest.raises(SystemExit):
            zoom_video.verify_output(empty, _spec(), 4.0, want_audio=False)


class TestSkillIsInstallable:
    def test_frontmatter_declares_the_ffmpeg_requirement(self) -> None:
        """Without this the skill would be offered on a host that cannot run it."""
        from nanobot.agent.skills import SkillsLoader

        text = (SKILL_DIR / "SKILL.md").read_text()
        assert text.startswith("---")
        frontmatter = text.split("---", 2)[1]
        assert "photo-zoom-video" in frontmatter
        metadata = json.loads(frontmatter.split("metadata:", 1)[1].strip().splitlines()[0])
        assert metadata["nanobot"]["requires"]["bins"] == ["ffmpeg", "ffprobe"]
        assert SkillsLoader  # imported to assert the loader contract exists

    def test_the_description_triggers_on_a_french_request(self) -> None:
        """The user asks in French; a description that only says 'zoom video' misses it."""
        description = (SKILL_DIR / "SKILL.md").read_text().split("description:", 1)[1]
        description = description.split("\n", 1)[0].lower()

        assert "zoom" in description
        assert "vidéo zoom" in description or "vidéo" in description

    def test_the_loader_lists_the_skill_when_ffmpeg_is_present(self, tmp_path: Path) -> None:
        from nanobot.agent.skills import SkillsLoader

        workspace = tmp_path / "ws"
        (workspace / "skills").mkdir(parents=True)
        shutil.copytree(SKILL_DIR, workspace / "skills" / "photo-zoom-video")

        loader = SkillsLoader(workspace=workspace)
        available, missing = loader.get_skill_availability("photo-zoom-video")

        if HAS_FFMPEG:
            assert available, missing
            assert any(s["name"] == "photo-zoom-video" for s in loader.list_skills())
        else:
            assert not available
            assert "ffmpeg" in missing

    def test_the_delivery_path_is_documented(self) -> None:
        """A local mp4 reaches the user only via the message tool's media param."""
        text = (SKILL_DIR / "SKILL.md").read_text()

        assert "message" in text and "media" in text

    def test_the_dockerfile_ships_ffmpeg(self) -> None:
        assert "ffmpeg" in (REPO_ROOT / "Dockerfile").read_text()
