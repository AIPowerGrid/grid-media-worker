"""Detect or install ComfyUI. Used by the setup wizard."""

import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)
_PROHIBITED_METADATA_ADDRESSES = {
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def validated_comfyui_url(value: object) -> str:
    """Validate an operator-owned ComfyUI API endpoint before local probing."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        raise ValueError("ComfyUI URL is required and must be at most 2048 characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError("ComfyUI URL must not contain control characters")
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ComfyUI URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("ComfyUI URL must use http or https and contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ComfyUI URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("ComfyUI URL must not contain a query string or fragment")
    if host in {"metadata", "metadata.google.internal"} or host.endswith(
        ".metadata.google.internal"
    ):
        raise ValueError("ComfyUI URL must not target a cloud metadata service")

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        addresses.add(ipaddress.ip_address(host))
    except ValueError:
        try:
            for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                addresses.add(ipaddress.ip_address(item[4][0]))
        except socket.gaierror as exc:
            raise ValueError("ComfyUI host could not be resolved") from exc
    if not addresses or any(
        address in _PROHIBITED_METADATA_ADDRESSES
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        for address in addresses
    ):
        raise ValueError("ComfyUI URL resolves to a prohibited address")

    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass
class DetectionResult:
    found: bool = False
    base_path: str | None = None
    url: str | None = None
    url_reachable: bool = False
    comfy_cli_available: bool = False
    comfy_cli_path: str | None = None
    comfy_cli_workspace: str | None = None
    methods: list[str] = field(default_factory=list)


def detect_comfyui() -> DetectionResult:
    """Run all detection heuristics and return a result."""
    result = DetectionResult()

    # 1. Find comfy-cli (system PATH, sibling venvs, known locations)
    comfy_bin = _find_comfy_cli()
    if comfy_bin:
        result.comfy_cli_available = True
        result.comfy_cli_path = comfy_bin
        result.methods.append(f"comfy-cli: {comfy_bin}")
        workspace = _get_comfy_cli_workspace(comfy_bin)
        if workspace:
            result.comfy_cli_workspace = workspace
            result.base_path = workspace
            result.found = True
            result.methods.append(f"comfy-cli workspace: {workspace}")

    # 2. Check env var
    env_path = os.environ.get("COMFYUI_BASE_PATH")
    if env_path and _looks_like_comfyui(env_path):
        result.base_path = env_path
        result.found = True
        result.methods.append(f"COMFYUI_BASE_PATH env: {env_path}")

    # 3. Scan common locations
    if not result.base_path:
        for candidate in _candidate_paths():
            if _looks_like_comfyui(str(candidate)):
                result.base_path = str(candidate)
                result.found = True
                result.methods.append(f"found at: {candidate}")
                break

    # 4. If we found a base_path, check for comfy-cli inside its venv
    if result.base_path and not result.comfy_cli_available:
        venv_comfy = _find_comfy_in_venv(result.base_path)
        if venv_comfy:
            result.comfy_cli_available = True
            result.comfy_cli_path = venv_comfy
            result.methods.append(f"comfy-cli in venv: {venv_comfy}")

    # 5. Derive URL default
    if result.found and not result.url:
        result.url = "http://127.0.0.1:8188"

    return result


async def check_comfyui_url(url: str) -> bool:
    """Ping the ComfyUI API to see if it's reachable."""
    try:
        url = validated_comfyui_url(url)
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{url}/system_stats")
            return resp.status_code == 200
    except Exception:
        return False


def install_comfyui_via_cli(install_path: str | None = None) -> dict:
    """Run the server-discovered comfy-cli executable to install ComfyUI."""
    comfy = _find_comfy_cli()
    if not comfy:
        return {"ok": False, "error": "comfy-cli not found. Install it first."}

    try:
        target = _validated_install_path(install_path)
    except ValueError:
        return {"ok": False, "error": "Invalid ComfyUI install path"}

    cmd = [comfy, "install", "--skip-prompt"]
    cmd.extend(["--path", str(target)])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode == 0:
            return {"ok": True}
        logger.error("ComfyUI installation failed with status %s", proc.returncode)
        return {"ok": False, "error": "ComfyUI installation failed; see local worker logs"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Installation timed out (10 min)"}
    except Exception:
        logger.exception("ComfyUI installation failed")
        return {"ok": False, "error": "ComfyUI installation failed; see local worker logs"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validated_install_path(value: str | None) -> Path:
    """Keep browser-initiated installs inside the operator's home directory."""
    home = Path.home().resolve()
    target = Path(value or home / "ComfyUI").expanduser().resolve()
    if target == home or home not in target.parents:
        raise ValueError("ComfyUI install path must be a directory inside your home folder")
    if target.exists() and not target.is_dir():
        raise ValueError("ComfyUI install path must be a directory")
    return target

def _find_comfy_cli() -> str | None:
    """Find the comfy binary — system PATH, our venv, or sibling venvs."""
    # System PATH
    found = shutil.which("comfy")
    if found:
        return found

    # Same bin dir as the running Python (e.g. bridge's own venv)
    bin_dir = Path(sys.executable).parent
    for name in ["comfy", "comfy.exe"]:
        candidate = bin_dir / name
        if candidate.exists():
            return str(candidate)

    # Check sibling/parent venvs (e.g. ../venv/bin/comfy for ComfyUI next door)
    cwd = Path.cwd()
    for venv_root in [cwd.parent, cwd.parent / "ComfyUI", Path.home() / "ComfyUI"]:
        c = _find_comfy_in_venv(str(venv_root))
        if c:
            return c

    return None


def _find_comfy_in_venv(base: str) -> str | None:
    """Check if comfy-cli exists in a venv under the given path."""
    p = Path(base)
    for bin_path in [p / "venv" / "bin" / "comfy", p / "venv" / "Scripts" / "comfy.exe"]:
        if bin_path.exists():
            return str(bin_path)
    return None


def _get_comfy_cli_workspace(comfy_bin: str) -> str | None:
    """Try to get the ComfyUI workspace path from comfy-cli."""
    # Try reading comfy-cli config files
    import json
    config_paths = [
        Path.home() / ".config" / "comfy-cli" / "config.json",
        Path.home() / ".comfy" / "config.json",
    ]
    for cfg_path in config_paths:
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text())
                workspace = data.get("workspace") or data.get("default_workspace")
                if workspace and Path(workspace).is_dir():
                    return str(workspace)
            except Exception:
                pass

    # Fallback: run `comfy env` and parse output
    try:
        proc = subprocess.run(
            [comfy_bin, "env"],
            capture_output=True, text=True, timeout=10,
        )
        for line in proc.stdout.splitlines():
            if "workspace" in line.lower() or "comfyui" in line.lower():
                parts = line.split(":", 1)
                if len(parts) == 2:
                    candidate = parts[1].strip()
                    if Path(candidate).is_dir():
                        return candidate
    except Exception:
        pass

    return None


def _looks_like_comfyui(path: str) -> bool:
    """Heuristic: does this directory look like a ComfyUI install?"""
    p = Path(path)
    if not p.is_dir():
        return False
    has_models = (p / "models").is_dir()
    if not has_models:
        return False
    # Classic install: main.py or server.py at root
    has_entry = (p / "main.py").exists() or (p / "server.py").exists()
    # comfy-cli managed: custom_nodes/ + models/
    has_custom_nodes = (p / "custom_nodes").is_dir()
    # comfy in the venv
    has_venv_comfy = bool(_find_comfy_in_venv(path))
    return has_entry or has_custom_nodes or has_venv_comfy


def _candidate_paths() -> list[Path]:
    """Common places where ComfyUI might be installed."""
    home = Path.home()
    cwd = Path.cwd()
    candidates = [
        cwd.parent,                # sibling of bridge install
        cwd.parent / "ComfyUI",
        home / "ComfyUI",
        home / "comfyui",
    ]
    # Platform-specific
    if sys.platform == "win32":
        candidates.extend([
            Path("C:/ComfyUI"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "ComfyUI",
        ])
    else:
        candidates.append(Path("/opt/ComfyUI"))
    # Parent directories up to 3 levels
    p = cwd
    for _ in range(3):
        p = p.parent
        candidates.append(p / "ComfyUI")
        if _looks_like_comfyui(str(p)):
            candidates.insert(0, p)
    return candidates
