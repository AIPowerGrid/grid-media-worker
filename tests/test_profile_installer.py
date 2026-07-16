# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import io
import os
import tarfile

import httpx
import pytest
import respx

from bridge.profiles.installer import InstallError, ProfileInstaller, download_verified


def _source_archive(files, *, extra_members=()):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for member in extra_members:
            archive.addfile(member)
    return output.getvalue()


def _archive_profile(content, *, unpacked_size, lock=b"locked"):
    return {
        "runtime": {
            "adapter": "ace-step-1.5-api",
            "python": "3.12.13",
            "source_artifact": "runtime",
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
        },
        "artifacts": [
            {
                "id": "runtime",
                "kind": "tar_archive",
                "source": "https://artifacts.example/runtime.tar.gz",
                "revision": "1" * 40,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "unpacked_size": unpacked_size,
                "strip_components": 1,
                "destination": "runtimes/ace",
            }
        ],
    }


@pytest.mark.asyncio
@respx.mock
async def test_download_verified_resumes_partial_file(tmp_path):
    content = b"verified-artifact"
    destination = tmp_path / "artifact.bin"
    partial = tmp_path / "artifact.bin.part"
    partial.write_bytes(content[:8])
    route = respx.get(
        "https://artifacts.example/model.bin",
        headers={"Range": "bytes=8-"},
    ).mock(
        return_value=httpx.Response(
            206,
            content=content[8:],
            headers={"Content-Range": f"bytes 8-{len(content) - 1}/{len(content)}"},
        )
    )
    progress = []

    async with httpx.AsyncClient() as client:
        status = await download_verified(
            client,
            "https://artifacts.example/model.bin",
            destination,
            expected_size=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
            progress=lambda completed, total: progress.append((completed, total)),
        )

    assert route.called
    assert status == "downloaded"
    assert destination.read_bytes() == content
    assert not partial.exists()
    assert progress[0] == (8, len(content))
    assert progress[-1] == (len(content), len(content))


@pytest.mark.asyncio
@respx.mock
async def test_download_restarts_when_server_ignores_range(tmp_path):
    content = b"complete-body"
    destination = tmp_path / "artifact.bin"
    partial = tmp_path / "artifact.bin.part"
    partial.write_bytes(b"stale")
    respx.get("https://artifacts.example/model.bin").mock(
        return_value=httpx.Response(200, content=content)
    )

    async with httpx.AsyncClient() as client:
        await download_verified(
            client,
            "https://artifacts.example/model.bin",
            destination,
            expected_size=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )

    assert destination.read_bytes() == content


@pytest.mark.asyncio
@respx.mock
async def test_download_rejects_hash_mismatch(tmp_path):
    content = b"wrong-content"
    respx.get("https://artifacts.example/model.bin").mock(
        return_value=httpx.Response(200, content=content)
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(InstallError, match="commitment mismatch"):
            await download_verified(
                client,
                "https://artifacts.example/model.bin",
                tmp_path / "artifact.bin",
                expected_size=len(content),
                expected_sha256="0" * 64,
            )

    assert not (tmp_path / "artifact.bin").exists()


@pytest.mark.asyncio
async def test_existing_verified_file_skips_network(tmp_path):
    content = b"already-present"
    destination = tmp_path / "artifact.bin"
    destination.write_bytes(content)

    async with httpx.AsyncClient() as client:
        status = await download_verified(
            client,
            "https://unreachable.invalid/model.bin",
            destination,
            expected_size=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )

    assert status == "verified"


def test_installer_rejects_symlink_escape(tmp_path):
    install_root = tmp_path / "install"
    outside = tmp_path / "outside"
    install_root.mkdir()
    outside.mkdir()
    (install_root / "runtimes").symlink_to(outside, target_is_directory=True)
    installer = ProfileInstaller(install_root)

    with pytest.raises(InstallError, match="escapes"):
        installer._destination("runtimes/model.bin")


def test_comfyui_prefix_maps_only_inside_bound_root(tmp_path):
    install_root = tmp_path / "install"
    comfyui_root = tmp_path / "ComfyUI"
    installer = ProfileInstaller(install_root, comfyui_root=comfyui_root)

    destination = installer._destination("comfyui/custom_nodes/ACE-Step")

    assert destination == comfyui_root / "custom_nodes" / "ACE-Step"


def test_comfyui_artifact_requires_explicit_bound_root(tmp_path):
    installer = ProfileInstaller(tmp_path / "install")

    with pytest.raises(InstallError, match="existing ComfyUI root"):
        installer._destination("comfyui/custom_nodes/ACE-Step")


def test_runtime_tool_status_is_boolean_and_privacy_safe(monkeypatch):
    monkeypatch.setattr("bridge.profiles.installer._uv_executable", lambda: "/private/uv")
    monkeypatch.setattr("bridge.profiles.installer.shutil.which", lambda name: "/private/git")
    profile = {"artifacts": [{"kind": "git"}]}

    assert ProfileInstaller.runtime_tool_status(profile) == {"uv": True, "git": True}


def test_archive_profile_does_not_require_system_git(monkeypatch):
    monkeypatch.setattr("bridge.profiles.installer._uv_executable", lambda: "/private/uv")
    monkeypatch.setattr("bridge.profiles.installer.shutil.which", lambda _name: None)
    profile = {"artifacts": [{"kind": "tar_archive"}]}

    assert ProfileInstaller.runtime_tool_status(profile) == {"uv": True, "git": True}


@pytest.mark.asyncio
@respx.mock
async def test_tar_archive_installs_atomically_and_then_uses_marker(tmp_path):
    files = {
        "source-root/uv.lock": b"locked",
        "source-root/src/runtime.py": b"print('pinned')\n",
    }
    content = _source_archive(files)
    profile = _archive_profile(
        content,
        unpacked_size=sum(len(value) for value in files.values()),
    )
    route = respx.get("https://artifacts.example/runtime.tar.gz").mock(
        return_value=httpx.Response(200, content=content)
    )
    installer = ProfileInstaller(tmp_path / "install")

    first = await installer.install(profile)
    second = await installer.install(profile)

    runtime = tmp_path / "install" / "runtimes" / "ace"
    assert first[0].status == "downloaded"
    assert second[0].status == "verified"
    assert (runtime / "uv.lock").read_bytes() == b"locked"
    assert (runtime / "src" / "runtime.py").read_bytes() == b"print('pinned')\n"
    assert (runtime / ".aipg-artifact.json").is_file()
    assert route.call_count == 1
    assert not list(runtime.parent.glob(".ace.extracting-*"))

    installer.verify_installed(profile)
    (runtime / "src" / "runtime.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(InstallError, match="runtime source drifted"):
        installer.verify_installed(profile)


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_revalidation_rejects_uncommitted_files(tmp_path):
    files = {"source-root/uv.lock": b"locked"}
    archive = _source_archive(files)
    profile = _archive_profile(archive, unpacked_size=len(b"locked"))
    model = b"pinned-model"
    profile["artifacts"].append(
        {
            "id": "models",
            "kind": "huggingface_snapshot",
            "source": "https://huggingface.co/example/models",
            "revision": "2" * 40,
            "destination": "runtimes/ace/checkpoints",
            "files": [
                {
                    "path": "model.safetensors",
                    "source": "https://raw.example/pinned-model.bin",
                    "size": len(model),
                    "sha256": hashlib.sha256(model).hexdigest(),
                }
            ],
        }
    )
    respx.get("https://artifacts.example/runtime.tar.gz").mock(
        return_value=httpx.Response(200, content=archive)
    )
    override = respx.get("https://raw.example/pinned-model.bin").mock(
        return_value=httpx.Response(200, content=model)
    )
    installer = ProfileInstaller(tmp_path / "install")

    await installer.install(profile)
    assert override.called
    installer.verify_installed(profile)
    checkpoint_root = tmp_path / "install" / "runtimes" / "ace" / "checkpoints"
    (checkpoint_root / "uncommitted.bin").write_bytes(b"network mutation")

    with pytest.raises(InstallError, match="uncommitted files"):
        installer.verify_installed(profile)


@pytest.mark.asyncio
@respx.mock
async def test_tar_archive_rejects_traversal_and_cleans_staging(tmp_path):
    files = {
        "source-root/uv.lock": b"locked",
        "source-root/../../outside": b"escape",
    }
    content = _source_archive(files)
    profile = _archive_profile(
        content,
        unpacked_size=sum(len(value) for value in files.values()),
    )
    respx.get("https://artifacts.example/runtime.tar.gz").mock(
        return_value=httpx.Response(200, content=content)
    )
    installer = ProfileInstaller(tmp_path / "install")

    with pytest.raises(InstallError, match="escapes"):
        await installer.install(profile)

    assert not (tmp_path / "outside").exists()
    assert not list((tmp_path / "install" / "runtimes").glob(".ace.extracting-*"))


@pytest.mark.asyncio
@respx.mock
async def test_tar_archive_rejects_links(tmp_path):
    link = tarfile.TarInfo("source-root/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    files = {"source-root/uv.lock": b"locked"}
    content = _source_archive(files, extra_members=(link,))
    profile = _archive_profile(content, unpacked_size=len(b"locked"))
    respx.get("https://artifacts.example/runtime.tar.gz").mock(
        return_value=httpx.Response(200, content=content)
    )

    with pytest.raises(InstallError, match="unsafe member"):
        await ProfileInstaller(tmp_path / "install").install(profile)

    assert not (tmp_path / "outside").exists()


@pytest.mark.asyncio
async def test_runtime_setup_uses_frozen_uv_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_API_KEY", "must-not-reach-package-builds")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-package-builds")
    runtime_root = tmp_path / "install" / "runtimes" / "ace"
    runtime_root.mkdir(parents=True)
    lock = runtime_root / "uv.lock"
    lock.write_bytes(b"locked")
    profile = {
        "runtime": {
            "adapter": "ace-step-1.5-api",
            "python": "3.12.13",
            "source_artifact": "runtime",
            "lock_sha256": hashlib.sha256(b"locked").hexdigest(),
        },
        "artifacts": [
            {
                "id": "runtime",
                "destination": "runtimes/ace",
            }
        ],
    }
    calls = []
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    expected_entrypoint = runtime_root / ".venv" / scripts / f"acestep-api{suffix}"

    def fake_run(command, working_directory, environment):
        calls.append((command, working_directory, environment))
        expected_entrypoint.parent.mkdir(parents=True)
        expected_entrypoint.write_text("", encoding="utf-8")

    monkeypatch.setattr("bridge.profiles.installer._uv_executable", lambda: "/usr/bin/uv")
    monkeypatch.setattr("bridge.profiles.installer._run_process", fake_run)
    installer = ProfileInstaller(tmp_path / "install")

    entrypoint = await installer.setup_runtime(profile)

    assert entrypoint == expected_entrypoint
    assert len(calls) == 1
    command, working_directory, environment = calls[0]
    assert command == [
        "/usr/bin/uv",
        "sync",
        "--frozen",
        "--no-dev",
        "--python",
        "3.12.13",
    ]
    assert working_directory == runtime_root
    assert environment["UV_CACHE_DIR"] == str(tmp_path / "install" / ".cache" / "uv")
    assert environment["UV_PYTHON_INSTALL_DIR"] == str(
        tmp_path / "install" / "runtimes" / ".python"
    )
    assert environment["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert "GRID_API_KEY" not in environment
