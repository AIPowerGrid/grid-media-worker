# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verified, resumable artifact installation for Worker Profile V1."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import quote

import httpx


SETUP_ENV_ALLOWLIST = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SystemRoot",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


class InstallError(RuntimeError):
    """An artifact could not be installed without violating its commitment."""


@dataclass(frozen=True)
class InstalledArtifact:
    id: str
    destination: Path
    status: str
    files_verified: int


class ProfileInstaller:
    """Install only the constrained artifact types defined by Profile V1."""

    def __init__(
        self,
        install_root: str | Path,
        *,
        comfyui_root: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.comfyui_root = (
            Path(comfyui_root).expanduser().resolve() if comfyui_root else None
        )
        self._client = client
        self._progress = progress

    async def install(self, profile: Mapping[str, Any]) -> tuple[InstalledArtifact, ...]:
        """Install and verify every artifact in declaration order."""

        results: list[InstalledArtifact] = []
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=300.0),
        )
        try:
            for artifact in profile["artifacts"]:
                kind = artifact["kind"]
                if kind == "http":
                    result = await self._install_http(client, artifact)
                elif kind == "tar_archive":
                    result = await self._install_tar_archive(client, artifact)
                elif kind == "huggingface_snapshot":
                    result = await self._install_huggingface(client, artifact)
                elif kind == "git":
                    result = await self._install_git(artifact)
                else:  # schema validation should make this unreachable
                    raise InstallError(f"unsupported artifact kind: {kind}")
                results.append(result)

            self._verify_runtime_lock(profile)
            return tuple(results)
        finally:
            if owns_client:
                await client.aclose()

    def verify_installed(self, profile: Mapping[str, Any]) -> None:
        """Revalidate the signed artifact commitments without using the network."""

        for artifact in profile["artifacts"]:
            kind = artifact["kind"]
            destination = self._destination(artifact["destination"])
            if kind == "http":
                if destination.is_symlink() or not _matches(
                    destination, artifact["size"], artifact["sha256"]
                ):
                    raise InstallError(f"installed artifact drifted: {artifact['id']}")
            elif kind == "tar_archive":
                archive_name = f"{artifact['id']}-{artifact['sha256']}.tar.gz"
                archive_path = _contained(
                    self.install_root, Path(".downloads") / archive_name
                )
                _verify_tar_archive_install(archive_path, destination, artifact)
            elif kind == "huggingface_snapshot":
                _verify_huggingface_install(destination, artifact)
            elif kind == "git":
                raise InstallError("git artifacts do not support offline revalidation")
            else:
                raise InstallError(f"unsupported artifact kind: {kind}")
        self._verify_runtime_lock(profile)

    def require_runtime_tools(self, profile: Mapping[str, Any]) -> None:
        """Fail before large downloads when the pinned runtime cannot be built."""
        if profile["runtime"]["adapter"] == "ace-step-1.5-api" and not _uv_executable():
            raise InstallError("uv is required to create the ACE-Step runtime")

    @staticmethod
    def runtime_tool_status(profile: Mapping[str, Any]) -> Mapping[str, bool]:
        """Report local bootstrap readiness without exposing filesystem paths."""
        return {
            "uv": _uv_executable() is not None,
            "git": not any(item["kind"] == "git" for item in profile["artifacts"])
            or shutil.which("git") is not None,
        }

    async def setup_runtime(self, profile: Mapping[str, Any]) -> Path:
        """Create the pinned ACE-Step environment using its committed uv lock."""

        runtime = profile["runtime"]
        if runtime["adapter"] != "ace-step-1.5-api":
            raise InstallError(f"unsupported runtime adapter: {runtime['adapter']}")
        self.require_runtime_tools(profile)
        uv = _uv_executable()
        if uv is None:  # defensive if PATH changes after the preflight check
            raise InstallError("uv is required to create the ACE-Step runtime")
        source = next(
            (
                item
                for item in profile["artifacts"]
                if item["id"] == runtime["source_artifact"]
            ),
            None,
        )
        if source is None:
            raise InstallError("runtime source artifact is not defined")
        runtime_root = self._destination(source["destination"])
        self._verify_runtime_lock(profile)
        await asyncio.to_thread(
            _run_process,
            [
                uv,
                "sync",
                "--frozen",
                "--no-dev",
                "--python",
                runtime["python"],
            ],
            runtime_root,
            self._setup_environment(),
        )
        scripts = "Scripts" if os.name == "nt" else "bin"
        suffix = ".exe" if os.name == "nt" else ""
        entrypoint = runtime_root / ".venv" / scripts / f"acestep-api{suffix}"
        if not entrypoint.is_file():
            raise InstallError(f"ACE-Step API entrypoint was not installed: {entrypoint}")
        return entrypoint

    def _setup_environment(self) -> Mapping[str, str]:
        """Keep package setup reproducible and isolated from operator secrets."""
        environment = {
            key: value for key, value in os.environ.items() if key in SETUP_ENV_ALLOWLIST
        }
        environment.update(
            {
                "UV_CACHE_DIR": str(self.install_root / ".cache" / "uv"),
                "UV_PYTHON_INSTALL_DIR": str(
                    self.install_root / "runtimes" / ".python"
                ),
                "UV_PYTHON_PREFERENCE": "only-managed",
            }
        )
        return environment

    async def _install_http(
        self,
        client: httpx.AsyncClient,
        artifact: Mapping[str, Any],
    ) -> InstalledArtifact:
        destination = self._destination(artifact["destination"])
        status = await download_verified(
            client,
            artifact["source"],
            destination,
            expected_size=artifact["size"],
            expected_sha256=artifact["sha256"],
            progress=self._progress_callback(artifact["id"]),
        )
        return InstalledArtifact(artifact["id"], destination, status, 1)

    async def _install_huggingface(
        self,
        client: httpx.AsyncClient,
        artifact: Mapping[str, Any],
    ) -> InstalledArtifact:
        destination = self._destination(artifact["destination"])
        verified = 0
        downloaded = False
        source = artifact["source"].rstrip("/")
        revision = artifact["revision"]
        for file_spec in artifact["files"]:
            relative = _safe_relative(file_spec["path"])
            target = _contained(destination, relative)
            url = file_spec.get("source") or (
                f"{source}/resolve/{revision}/{quote(relative.as_posix(), safe='/')}"
            )
            status = await download_verified(
                client,
                url,
                target,
                expected_size=file_spec["size"],
                expected_sha256=file_spec["sha256"],
                progress=self._progress_callback(
                    f"{artifact['id']}:{relative.as_posix()}"
                ),
            )
            downloaded = downloaded or status == "downloaded"
            verified += 1
        return InstalledArtifact(
            artifact["id"],
            destination,
            "downloaded" if downloaded else "verified",
            verified,
        )

    async def _install_tar_archive(
        self,
        client: httpx.AsyncClient,
        artifact: Mapping[str, Any],
    ) -> InstalledArtifact:
        destination = self._destination(artifact["destination"])
        commitment = _archive_commitment(artifact)
        if _installed_archive_matches(destination, commitment):
            return InstalledArtifact(artifact["id"], destination, "verified", 1)

        archive_name = f"{artifact['id']}-{artifact['sha256']}.tar.gz"
        archive_path = _contained(self.install_root, Path(".downloads") / archive_name)
        await download_verified(
            client,
            artifact["source"],
            archive_path,
            expected_size=artifact["size"],
            expected_sha256=artifact["sha256"],
            progress=self._progress_callback(artifact["id"]),
        )
        await asyncio.to_thread(
            _extract_tar_archive,
            archive_path,
            destination,
            commitment,
            strip_components=artifact["strip_components"],
            expected_unpacked_size=artifact["unpacked_size"],
        )
        return InstalledArtifact(artifact["id"], destination, "downloaded", 1)

    async def _install_git(self, artifact: Mapping[str, Any]) -> InstalledArtifact:
        destination = self._destination(artifact["destination"])
        revision = artifact["revision"]
        source = artifact["source"]
        await asyncio.to_thread(_checkout_git_revision, source, revision, destination)
        return InstalledArtifact(artifact["id"], destination, "verified", 1)

    def _progress_callback(
        self,
        label: str,
    ) -> Callable[[int, int], None] | None:
        if self._progress is None:
            return None
        return lambda completed, total: self._progress(label, completed, total)

    def _verify_runtime_lock(self, profile: Mapping[str, Any]) -> None:
        runtime = profile["runtime"]
        source_id = runtime["source_artifact"]
        source = next(
            (item for item in profile["artifacts"] if item["id"] == source_id),
            None,
        )
        if source is None:
            raise InstallError(f"runtime source artifact is missing: {source_id}")
        lock_path = self._destination(source["destination"]) / "uv.lock"
        if not lock_path.is_file():
            raise InstallError(f"runtime lockfile is missing: {lock_path}")
        actual = _sha256_file(lock_path)
        expected = runtime["lock_sha256"]
        if actual != expected:
            raise InstallError(
                f"runtime lockfile hash mismatch: expected {expected}, got {actual}"
            )

    def _destination(self, relative_value: str) -> Path:
        relative = _safe_relative(relative_value)
        if relative.parts[0] == "comfyui":
            if self.comfyui_root is None:
                raise InstallError("this profile requires an existing ComfyUI root")
            if len(relative.parts) == 1:
                return self.comfyui_root
            return _contained(self.comfyui_root, Path(*relative.parts[1:]))
        return _contained(self.install_root, relative)


async def download_verified(
    client: httpx.AsyncClient,
    url: str,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Resume an HTTPS download and atomically promote it after verification."""

    if not url.startswith("https://"):
        raise InstallError("artifact downloads require HTTPS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _matches(destination, expected_size, expected_sha256):
        if progress:
            progress(expected_size, expected_size)
        return "verified"
    if destination.exists():
        if destination.is_dir():
            raise InstallError(f"artifact destination is a directory: {destination}")
        destination.unlink()

    partial = destination.with_name(destination.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > expected_size:
        partial.unlink()
        offset = 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    if progress:
        progress(offset, expected_size)

    async with client.stream("GET", url, headers=headers) as response:
        if offset and response.status_code == 416 and offset == expected_size:
            pass
        else:
            response.raise_for_status()
            append = offset > 0 and response.status_code == 206
            if append:
                content_range = response.headers.get("content-range", "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise InstallError(f"server returned an invalid Content-Range for {url}")
            else:
                offset = 0
                if progress:
                    progress(0, expected_size)
            mode = "ab" if append else "wb"
            written = offset
            with partial.open(mode) as handle:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > expected_size:
                        raise InstallError(f"artifact exceeds committed size: {url}")
                    handle.write(chunk)
                    if progress:
                        progress(written, expected_size)
                handle.flush()
                os.fsync(handle.fileno())

    if not _matches(partial, expected_size, expected_sha256):
        actual_size = partial.stat().st_size if partial.exists() else 0
        actual_hash = _sha256_file(partial) if partial.exists() else "missing"
        raise InstallError(
            f"artifact commitment mismatch for {url}: size={actual_size}, sha256={actual_hash}"
        )
    os.replace(partial, destination)
    if progress:
        progress(expected_size, expected_size)
    return "downloaded"


def _checkout_git_revision(source: str, revision: str, destination: Path) -> None:
    if not source.startswith("https://"):
        raise InstallError("git artifacts require HTTPS")
    git = shutil.which("git")
    if not git:
        raise InstallError("git is required to install this profile")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not (destination / ".git").is_dir():
        if any(destination.iterdir()):
            raise InstallError(f"git destination is not an existing checkout: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    if not (destination / ".git").is_dir():
        _run_git([git, "init", str(destination)])
        _run_git([git, "-C", str(destination), "remote", "add", "origin", source])
    else:
        remote = _run_git(
            [git, "-C", str(destination), "remote", "get-url", "origin"],
            capture=True,
        ).strip()
        if remote != source:
            raise InstallError(
                f"git origin mismatch at {destination}: expected {source}, got {remote}"
            )

    _run_git(
        [git, "-C", str(destination), "fetch", "--depth", "1", "origin", revision]
    )
    _run_git([git, "-C", str(destination), "checkout", "--detach", "--force", "FETCH_HEAD"])
    actual = _run_git(
        [git, "-C", str(destination), "rev-parse", "HEAD"], capture=True
    ).strip()
    if actual != revision:
        raise InstallError(
            f"git revision mismatch at {destination}: expected {revision}, got {actual}"
        )


def _run_git(command: list[str], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise InstallError(f"git operation failed: {detail.strip()}") from exc
    return result.stdout if capture else ""


def _run_process(
    command: list[str],
    working_directory: Path,
    environment: Mapping[str, str],
) -> None:
    try:
        subprocess.run(
            command,
            cwd=working_directory,
            env=dict(environment),
            check=True,
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"runtime setup failed: {exc}") from exc


ARCHIVE_MARKER = ".aipg-artifact.json"
MAX_ARCHIVE_MEMBERS = 20_000


def _archive_commitment(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "version": 1,
        "id": artifact["id"],
        "kind": artifact["kind"],
        "source": artifact["source"],
        "revision": artifact["revision"],
        "sha256": artifact["sha256"],
        "size": artifact["size"],
        "unpacked_size": artifact["unpacked_size"],
        "strip_components": artifact["strip_components"],
    }


def _installed_archive_matches(
    destination: Path,
    commitment: Mapping[str, Any],
) -> bool:
    marker = destination / ARCHIVE_MARKER
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return value == commitment


def _extract_tar_archive(
    archive_path: Path,
    destination: Path,
    commitment: Mapping[str, Any],
    *,
    strip_components: int,
    expected_unpacked_size: int,
) -> None:
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
            raise InstallError(
                f"archive destination is not an empty managed directory: {destination}"
            )
        destination.rmdir()
    staging = destination.with_name(
        f".{destination.name}.extracting-{commitment['sha256'][:12]}"
    )
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise InstallError(f"unsafe archive staging path: {staging}")
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        _extract_members(
            archive_path,
            staging,
            strip_components=strip_components,
            expected_unpacked_size=expected_unpacked_size,
        )
        marker = staging / ARCHIVE_MARKER
        marker.write_text(
            json.dumps(commitment, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(marker, 0o644)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _verify_tar_archive_install(
    archive_path: Path,
    destination: Path,
    artifact: Mapping[str, Any],
) -> None:
    commitment = _archive_commitment(artifact)
    if not _installed_archive_matches(destination, commitment):
        raise InstallError(f"installed archive marker drifted: {artifact['id']}")
    if archive_path.is_symlink() or not _matches(
        archive_path, artifact["size"], artifact["sha256"]
    ):
        raise InstallError(f"installed source archive drifted: {artifact['id']}")
    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"installed source archive is invalid: {exc}") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise InstallError("installed source archive has too many members")
        for member in members:
            relative = _stripped_archive_path(
                member.name, artifact["strip_components"]
            )
            if relative is None or member.isdir():
                continue
            if not member.isfile():
                raise InstallError(
                    f"installed source archive contains an unsafe member: {member.name}"
                )
            target = _contained(destination, relative)
            if target.is_symlink() or not target.is_file() or target.stat().st_size != member.size:
                raise InstallError(f"installed runtime source drifted: {relative.as_posix()}")
            source = archive.extractfile(member)
            if source is None:
                raise InstallError(f"installed source member cannot be read: {member.name}")
            with source:
                expected = _sha256_stream(source)
            if _sha256_file(target) != expected:
                raise InstallError(f"installed runtime source drifted: {relative.as_posix()}")


def _verify_huggingface_install(
    destination: Path,
    artifact: Mapping[str, Any],
) -> None:
    expected: set[str] = set()
    for file_spec in artifact["files"]:
        relative = _safe_relative(file_spec["path"])
        normalized = relative.as_posix()
        expected.add(normalized)
        target = _contained(destination, relative)
        if target.is_symlink() or not _matches(
            target, file_spec["size"], file_spec["sha256"]
        ):
            raise InstallError(f"installed model file drifted: {normalized}")

    actual: set[str] = set()
    if destination.is_symlink() or not destination.is_dir():
        raise InstallError(f"installed model directory is missing: {artifact['id']}")
    for root, directories, files in os.walk(destination, followlinks=False):
        root_path = Path(root)
        for name in directories:
            if (root_path / name).is_symlink():
                raise InstallError(f"installed model tree contains a symlink: {name}")
        for name in files:
            path = root_path / name
            if path.is_symlink():
                raise InstallError(f"installed model tree contains a symlink: {name}")
            actual.add(path.relative_to(destination).as_posix())
    unexpected = sorted(actual - expected)
    if unexpected:
        raise InstallError(f"installed model tree has uncommitted files: {unexpected[0]}")


def _extract_members(
    archive_path: Path,
    destination: Path,
    *,
    strip_components: int,
    expected_unpacked_size: int,
) -> None:
    try:
        archive = tarfile.open(archive_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise InstallError(f"source archive is invalid: {exc}") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise InstallError("source archive has too many members")
        prepared: list[tuple[tarfile.TarInfo, Path]] = []
        paths: set[str] = set()
        unpacked_size = 0
        for member in members:
            relative = _stripped_archive_path(member.name, strip_components)
            if relative is None:
                continue
            if relative.name == ARCHIVE_MARKER:
                raise InstallError("source archive contains a reserved manager path")
            if not (member.isdir() or member.isfile()):
                raise InstallError(f"source archive contains an unsafe member: {member.name}")
            target = _contained(destination, relative)
            normalized = os.path.normcase(str(target))
            if normalized in paths:
                raise InstallError(f"source archive contains a duplicate path: {member.name}")
            paths.add(normalized)
            if member.isfile():
                unpacked_size += member.size
                if unpacked_size > expected_unpacked_size:
                    raise InstallError("source archive exceeds its committed unpacked size")
            prepared.append((member, target))
        if unpacked_size != expected_unpacked_size:
            raise InstallError(
                "source archive unpacked size does not match its profile commitment"
            )

        for member, target in prepared:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=False, mode=0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise InstallError(f"source archive member cannot be read: {member.name}")
            written = 0
            with source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > member.size:
                        raise InstallError(f"source archive member grew: {member.name}")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if written != member.size:
                raise InstallError(f"source archive member is truncated: {member.name}")
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)


def _stripped_archive_path(value: str, strip_components: int) -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise InstallError("source archive contains an unsafe path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise InstallError(f"source archive path escapes its destination: {value}")
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if len(parts) <= strip_components:
        return None
    return Path(*parts[strip_components:])


def _uv_executable() -> str | None:
    """Prefer the manager-bundled uv, then a normal PATH installation."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        suffix = ".exe" if os.name == "nt" else ""
        bundled = Path(bundle_root) / "uv" / "bin" / f"uv{suffix}"
        if bundled.is_file():
            return str(bundled)
    return shutil.which("uv")


def _safe_relative(value: str) -> Path:
    if "\\" in value or "\x00" in value:
        raise InstallError(f"unsafe artifact destination: {value}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise InstallError(f"unsafe artifact destination: {value}")
    if pure.parts[0].endswith(":"):
        raise InstallError(f"unsafe artifact destination: {value}")
    return Path(*pure.parts)


def _contained(root: Path, relative: Path) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise InstallError(f"artifact path escapes installation root: {relative}") from exc
    return candidate


def _matches(path: Path, expected_size: int, expected_sha256: str) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == expected_size
        and _sha256_file(path) == expected_sha256
    )


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()
