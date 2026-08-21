"""Tests for the image-skill seeder.

The seeder exists because a container recycle once destroyed skills that lived
nowhere else. Its whole value is in two behaviours: it puts them back when they
are missing, and it never touches a copy that is already there (which is either
restored from the mirror or edited by the agent, and in both cases newer than
the image).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.persistence import seed_skills


def _source(root: Path, *names: str) -> Path:
    source = root / "seed"
    for name in names:
        (source / name / "scripts").mkdir(parents=True)
        (source / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")
        (source / name / "scripts" / "gen_image.py").write_text("print('run')\n")
    return source


def test_seeds_every_missing_skill(tmp_path: Path) -> None:
    source = _source(tmp_path, "agnes-image", "cloudflare-ai-image")
    target = tmp_path / "data" / "workspace" / "skills"

    seeded, skipped = seed_skills.seed(source, target)

    assert sorted(seeded) == ["agnes-image", "cloudflare-ai-image"]
    assert skipped == []
    assert (target / "agnes-image" / "SKILL.md").is_file()
    assert (target / "agnes-image" / "scripts" / "gen_image.py").is_file()


def test_existing_skill_is_never_overwritten(tmp_path: Path) -> None:
    source = _source(tmp_path, "agnes-image", "cloudflare-ai-image")
    target = tmp_path / "data" / "workspace" / "skills"
    (target / "agnes-image").mkdir(parents=True)
    (target / "agnes-image" / "SKILL.md").write_text("agent's own newer version")

    seeded, skipped = seed_skills.seed(source, target)

    assert seeded == ["cloudflare-ai-image"]
    assert skipped == ["agnes-image"]
    assert (target / "agnes-image" / "SKILL.md").read_text() == "agent's own newer version"


def test_seeding_twice_changes_nothing(tmp_path: Path) -> None:
    source = _source(tmp_path, "agnes-image")
    target = tmp_path / "data" / "workspace" / "skills"

    assert seed_skills.seed(source, target)[0] == ["agnes-image"]
    assert seed_skills.seed(source, target) == ([], ["agnes-image"])


def test_missing_or_empty_source_fails_loudly(tmp_path: Path) -> None:
    target = tmp_path / "data" / "workspace" / "skills"
    with pytest.raises(FileNotFoundError, match="not a directory"):
        seed_skills.seed(tmp_path / "absent", target)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="no skill directories"):
        seed_skills.seed(empty, target)


def test_no_secret_material_ships_in_the_seeded_skills() -> None:
    """The image copy must carry no credentials — keys come from the environment."""
    shipped = Path(__file__).resolve().parents[2] / "seed-skills"
    assert shipped.is_dir(), "the seed-skills tree must be committed"

    leaks: list[str] = []
    for path in shipped.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for marker in ("cfut_", "hf_Dil", "hf_PhG", "sk-or-v1-", "sk-Lme"):
            if marker in text:
                leaks.append(f"{path.name}: {marker}")
    assert leaks == [], f"credential-looking material in the shipped skills: {leaks}"


def test_main_reports_and_survives_a_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NANOBOT_SEED_SKILLS_DIR", str(tmp_path / "absent"))
    assert seed_skills.main() == 1

    monkeypatch.setenv("NANOBOT_SEED_SKILLS", "0")
    assert seed_skills.main() == 0
    assert "disabled" in capsys.readouterr().out
