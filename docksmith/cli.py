"""
Docksmith CLI entry point.

Usage:
  docksmith build  -t <name:tag> [--no-cache] <context>
  docksmith images
  docksmith rmi    <name:tag>
  docksmith run    [-e KEY=VAL ...] <name:tag> [cmd ...]
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import parser, store
from .builder import build
from .images import list_images, remove_image
from .runtime import run_isolated


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="docksmith",
        description="Simplified Docker-like build and runtime system",
    )
    sub = ap.add_subparsers(dest="command", metavar="<command>")

    # ── build ─────────────────────────────────────────────────────────────────
    p_build = sub.add_parser("build", help="Build an image from a Docksmithfile")
    p_build.add_argument("-t", dest="tag", required=True, metavar="name:tag",
                         help="Name and tag for the built image")
    p_build.add_argument("--no-cache", dest="no_cache", action="store_true",
                         help="Skip all cache lookups and writes")
    p_build.add_argument("context", metavar="<context>",
                         help="Build context directory")

    # ── images ────────────────────────────────────────────────────────────────
    sub.add_parser("images", help="List all images in the local store")

    # ── rmi ───────────────────────────────────────────────────────────────────
    p_rmi = sub.add_parser("rmi", help="Remove an image")
    p_rmi.add_argument("image", metavar="name:tag")

    # ── run ───────────────────────────────────────────────────────────────────
    p_run = sub.add_parser("run", help="Run a container")
    p_run.add_argument("-e", dest="env_overrides", action="append",
                       default=[], metavar="KEY=VALUE",
                       help="Override / add an environment variable (repeatable)")
    p_run.add_argument("image", metavar="name:tag")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER, metavar="[cmd ...]")

    args = ap.parse_args()

    if args.command is None:
        ap.print_help()
        sys.exit(1)

    try:
        _dispatch(args)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


# ── command handlers ──────────────────────────────────────────────────────────

def _dispatch(args) -> None:
    if args.command == "build":
        _cmd_build(args)
    elif args.command == "images":
        list_images()
    elif args.command == "rmi":
        _cmd_rmi(args)
    elif args.command == "run":
        _cmd_run(args)


def _cmd_build(args) -> None:
    context = Path(args.context).resolve()
    dsf = context / "Docksmithfile"
    if not dsf.exists():
        raise FileNotFoundError(f"No Docksmithfile found in {context}")

    content = dsf.read_text()
    instructions = parser.parse(content)

    if ":" in args.tag:
        name, tag = args.tag.rsplit(":", 1)
    else:
        name, tag = args.tag, "latest"

    build(
        instructions=instructions,
        context_dir=context,
        name=name,
        tag=tag,
        no_cache=args.no_cache,
    )


def _cmd_rmi(args) -> None:
    if ":" in args.image:
        name, tag = args.image.rsplit(":", 1)
    else:
        name, tag = args.image, "latest"
    remove_image(name, tag)


def _cmd_run(args) -> None:
    if ":" in args.image:
        name, tag = args.image.rsplit(":", 1)
    else:
        name, tag = args.image, "latest"

    manifest = store.load_manifest(name, tag)
    if manifest is None:
        raise RuntimeError(f"Image '{name}:{tag}' not found.")

    # Resolve command
    cmd_override = args.cmd if args.cmd else None
    cfg = manifest.get("config", {})
    base_cmd = cfg.get("Cmd") or []

    if cmd_override:
        cmd = list(cmd_override)
    elif base_cmd:
        cmd = list(base_cmd)
    else:
        raise RuntimeError(
            f"No CMD defined in image '{name}:{tag}' and no command given."
        )

    # Resolve environment
    env: dict[str, str] = {}
    for pair in cfg.get("Env", []):
        k, _, v = pair.partition("=")
        env[k] = v
    for override in args.env_overrides:
        k, _, v = override.partition("=")
        if not k:
            raise ValueError(f"Invalid -e value: {override!r}")
        env[k] = v

    workdir = cfg.get("WorkingDir") or "/"

    # Assemble rootfs in a temp dir, run, clean up
    with tempfile.TemporaryDirectory(prefix="docksmith_run_") as tmpdir:
        rootfs = Path(tmpdir) / "rootfs"
        rootfs.mkdir()

        for entry in manifest.get("layers", []):
            from .layers import extract_layer
            extract_layer(entry["digest"], store.LAYERS_DIR, rootfs)

        rc = run_isolated(rootfs, cmd, env, workdir)

    sys.exit(rc)
