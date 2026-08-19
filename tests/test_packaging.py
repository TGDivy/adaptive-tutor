from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SDIST_ROOT_FILES = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}
FORBIDDEN_FILENAMES = {
    ".dockerignore",
    ".env",
    "AGENTS.md",
    "GOAL.md",
    "credentials.json",
}
FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".pyc", ".pyo"}


def _copy_build_fixture(destination: Path) -> None:
    destination.mkdir()
    for filename in (".gitignore", "LICENSE", "README.md", "pyproject.toml"):
        shutil.copy2(ROOT / filename, destination / filename)
    for directory in ("curricula", "src"):
        shutil.copytree(
            ROOT / directory,
            destination / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    sentinels = {
        "AGENTS.md": "repository instructions",
        "GOAL.md": "private project intent",
        "implementation/completion.json": "{}",
        "src/adaptive_tutor/.env": "TOKEN=not-a-real-secret",
        "curricula/credentials.json": "{}",
    }
    for relative_path, content in sentinels.items():
        path = destination / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _assert_paths_are_public(paths: set[str]) -> None:
    for value in paths:
        path = PurePosixPath(value)
        assert path.parts and ".." not in path.parts, value
        assert "__pycache__" not in path.parts, value
        assert path.name not in FORBIDDEN_FILENAMES, value
        assert not path.name.startswith(".env."), value
        assert path.suffix.lower() not in FORBIDDEN_SUFFIXES, value


def _sdist_paths(archive_path: Path) -> set[str]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = [
            PurePosixPath(member.name) for member in archive.getmembers() if not member.isdir()
        ]

    roots = {member.parts[0] for member in members}
    assert len(roots) == 1
    return {PurePosixPath(*member.parts[1:]).as_posix() for member in members}


def _wheel_paths(archive_path: Path) -> set[str]:
    with zipfile.ZipFile(archive_path) as archive:
        return {name for name in archive.namelist() if not name.endswith("/")}


def test_built_artifacts_contain_only_public_product_files(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    output_dir = tmp_path / "dist"
    _copy_build_fixture(project_root)
    uv = shutil.which("uv")
    assert uv is not None
    result = subprocess.run(  # noqa: S603 - uv is resolved and arguments are fixed.
        [
            uv,
            "build",
            "--clear",
            "--no-create-gitignore",
            "--out-dir",
            str(output_dir),
            str(project_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    sdists = list(output_dir.glob("*.tar.gz"))
    wheels = list(output_dir.glob("*.whl"))
    assert len(sdists) == 1
    assert len(wheels) == 1

    sdist_paths = _sdist_paths(sdists[0])
    wheel_paths = _wheel_paths(wheels[0])
    _assert_paths_are_public(sdist_paths)
    _assert_paths_are_public(wheel_paths)

    assert all(
        path in SDIST_ROOT_FILES
        or path.startswith("src/adaptive_tutor/")
        or path.startswith("curricula/")
        for path in sdist_paths
    )
    assert all(
        path.startswith("adaptive_tutor/")
        or (
            path.split("/", maxsplit=1)[0].startswith("adaptive_tutor-")
            and path.split("/", maxsplit=1)[0].endswith(".dist-info")
        )
        for path in wheel_paths
    )

    assert "src/adaptive_tutor/__init__.py" in sdist_paths
    assert "curricula/systems-foundations/curriculum.yaml" in sdist_paths
    assert "adaptive_tutor/__init__.py" in wheel_paths
    assert "adaptive_tutor/bundled_curricula/systems-foundations/curriculum.yaml" in wheel_paths
    assert any(path.endswith(".dist-info/METADATA") for path in wheel_paths)
