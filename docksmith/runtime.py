"""
Container runtime – Linux process isolation.

Uses unshare(1) to create new user + mount + PID namespaces,
then chroot(8) to pivot the process into the assembled rootfs.

The SAME function is used for:
  • RUN instructions during build
  • docksmith run  (container start)

Requirements:
  • Linux kernel ≥ 3.12 with user-namespace support
  • unshare(1) and chroot(8) on host PATH  (util-linux, standard on all distros)
"""

import shlex
import subprocess
import sys
from pathlib import Path


# ── public API ────────────────────────────────────────────────────────────────

def run_isolated(
    rootfs: Path,
    cmd: list[str],
    env: dict[str, str],
    workdir: str = "/",
    *,
    stdin=None,
    stdout=None,
    stderr=None,
) -> int:
    """
    Run *cmd* inside *rootfs* with Linux namespace isolation.

    Returns the process exit code.
    Raises RuntimeError if not on Linux.
    """
    if sys.platform != "linux":
        raise RuntimeError("Container runtime requires Linux.")

    _ensure_dirs(rootfs)

    inner_sh = _build_inner_script(env, workdir, cmd)

    outer_cmd = [
        "unshare",
        "--user",          # new user namespace
        "--map-root-user", # map current uid → root inside namespace
        "--mount",         # new mount namespace (prevent host mount pollution)
        "--pid",           # new PID namespace
        "--fork",          # fork so the child is PID 1 in new PID ns
        "chroot", str(rootfs),
        "/bin/sh", "-c", inner_sh,
    ]

    result = subprocess.run(
        outer_cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    return result.returncode


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_dirs(rootfs: Path) -> None:
    """Create /proc and /tmp inside rootfs if missing (they may not exist in delta layers)."""
    for d in ("proc", "tmp", "dev"):
        (rootfs / d).mkdir(exist_ok=True, parents=True)


def _build_inner_script(
    env: dict[str, str],
    workdir: str,
    cmd: list[str],
) -> str:
    """
    Build the /bin/sh -c script that runs *inside* the chroot.

    Steps:
      1. Mount /proc (needed by many programs; ignore failure quietly).
      2. Export all ENV variables.
      3. cd to workdir.
      4. exec the target command (replaces the shell process).
    """
    parts: list[str] = [
        "mount -t proc proc /proc 2>/dev/null || true",
    ]

    # export env vars in deterministic order
    for k, v in sorted(env.items()):
        parts.append(f"export {k}={shlex.quote(v)}")

    wd = workdir if workdir else "/"
    parts.append(f"cd {shlex.quote(wd)} 2>/dev/null || cd /")

    cmd_str = " ".join(shlex.quote(c) for c in cmd)
    parts.append(f"exec {cmd_str}")

    return "; ".join(parts)
