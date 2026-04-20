"""
Build engine.

Processes a parsed Docksmithfile instruction list:
  FROM   – load base image, initialise layer stack
  COPY   – copy files from build context → delta tar layer
  RUN    – execute command in isolated rootfs → delta tar layer
  WORKDIR – update working-directory config (no layer)
  ENV    – update environment config (no layer)
  CMD    – set default command config (no layer)

Returns a completed image manifest dict on success.
"""

import hashlib
import shutil
import tempfile
import time
from pathlib import Path

from . import cache, layers, store
from .runtime import run_isolated


# ── public entry point ────────────────────────────────────────────────────────

def build(
    instructions: list[dict],
    context_dir: Path,
    name: str,
    tag: str,
    no_cache: bool = False,
) -> dict:
    """
    Execute *instructions* and return the final image manifest.
    Prints step lines to stdout.
    """
    store.init_store()
    cache_index = store.load_cache()

    # Build state
    base_manifest: dict | None = None
    prev_digest: str = ""          # digest feeding into next cache key
    layer_entries: list[dict] = [] # accumulated manifest layer list
    env: dict[str, str] = {}
    workdir: str = ""
    cmd: list | None = None

    cascade_miss = no_cache        # once True, all subsequent steps are misses
    total_steps = len(instructions)
    step_num = 0
    total_time = 0.0

    # Check for existing manifest (to preserve `created` on warm rebuild)
    existing_manifest = store.load_manifest(name, tag)
    all_hits = True  # will be updated as we process

    for instr_dict in instructions:
        instr = instr_dict["instruction"]
        arg = instr_dict["arg"]
        step_num += 1
        prefix = f"Step {step_num}/{total_steps} : {instr} {arg}"

        # ── FROM ──────────────────────────────────────────────────────────────
        if instr == "FROM":
            img_name, img_tag = _parse_image_ref(arg)
            base_manifest = store.load_manifest(img_name, img_tag)
            if base_manifest is None:
                raise RuntimeError(
                    f"Image '{img_name}:{img_tag}' not found in local store.\n"
                    f"Run setup_images.py first to import base images."
                )
            # Inherit base layers
            layer_entries = list(base_manifest.get("layers", []))
            # First cache key anchors on the base manifest digest
            prev_digest = base_manifest["digest"]
            # Inherit config defaults (but Docksmithfile overrides take precedence)
            base_cfg = base_manifest.get("config", {})
            env = dict(
                _parse_env_list(base_cfg.get("Env", []))
            )
            workdir = base_cfg.get("WorkingDir", "")
            cmd = base_cfg.get("Cmd")

            print(prefix)
            continue

        # ── WORKDIR ───────────────────────────────────────────────────────────
        if instr == "WORKDIR":
            workdir = arg.strip()
            print(prefix)
            continue

        # ── ENV ───────────────────────────────────────────────────────────────
        if instr == "ENV":
            k, _, v = arg.partition("=")
            env[k.strip()] = v.strip()
            print(prefix)
            continue

        # ── CMD ───────────────────────────────────────────────────────────────
        if instr == "CMD":
            import json as _json
            try:
                cmd = _json.loads(arg)
            except Exception:
                raise ValueError(f"CMD argument must be a JSON array: {arg}")
            print(prefix)
            continue

        # ── COPY ──────────────────────────────────────────────────────────────
        if instr == "COPY":
            import time as _t
            src_pattern, dest = _parse_copy_arg(arg)

            # Expand glob from context
            file_pairs, file_hashes = _expand_copy_sources(
                context_dir, src_pattern, dest
            )

            # Compute cache key
            ck = cache.compute_key(
                prev_digest=prev_digest,
                instruction_text=f"COPY {arg}",
                workdir=workdir,
                env=env,
                file_hashes=file_hashes,
            )

            hit_digest = None if cascade_miss else cache.lookup(ck, cache_index)

            t0 = _t.monotonic()
            if hit_digest:
                elapsed = _t.monotonic() - t0
                print(f"{prefix} [CACHE HIT] {elapsed:.2f}s")
                new_digest = hit_digest
            else:
                all_hits = False
                cascade_miss = True
                # Assemble rootfs, ensure WORKDIR exists, build COPY tar
                with tempfile.TemporaryDirectory(prefix="docksmith_copy_") as tmpdir:
                    rootfs = Path(tmpdir) / "rootfs"
                    rootfs.mkdir()
                    _assemble_rootfs(layer_entries, rootfs)
                    # Silent WORKDIR creation
                    if workdir:
                        (rootfs / workdir.lstrip("/")).mkdir(
                            parents=True, exist_ok=True
                        )
                    # Build delta tar
                    tar_bytes = layers.make_copy_tar(file_pairs)

                elapsed = _t.monotonic() - t0
                new_digest = store.write_layer(tar_bytes)
                if not no_cache:
                    cache.record(ck, new_digest, cache_index)
                    store.save_cache(cache_index)
                print(f"{prefix} [CACHE MISS] {elapsed:.2f}s")

            total_time += elapsed
            prev_digest = new_digest
            size = store.layer_file(new_digest).stat().st_size
            layer_entries.append({
                "digest": new_digest,
                "size": size,
                "createdBy": f"COPY {arg}",
            })
            continue

        # ── RUN ───────────────────────────────────────────────────────────────
        if instr == "RUN":
            import time as _t

            ck = cache.compute_key(
                prev_digest=prev_digest,
                instruction_text=f"RUN {arg}",
                workdir=workdir,
                env=env,
            )

            hit_digest = None if cascade_miss else cache.lookup(ck, cache_index)

            t0 = _t.monotonic()
            if hit_digest:
                elapsed = _t.monotonic() - t0
                print(f"{prefix} [CACHE HIT] {elapsed:.2f}s")
                new_digest = hit_digest
            else:
                all_hits = False
                cascade_miss = True
                new_digest = _execute_run(
                    arg, layer_entries, env, workdir
                )
                elapsed = _t.monotonic() - t0
                if not no_cache:
                    cache.record(ck, new_digest, cache_index)
                    store.save_cache(cache_index)
                print(f"{prefix} [CACHE MISS] {elapsed:.2f}s")

            total_time += elapsed
            prev_digest = new_digest
            size = store.layer_file(new_digest).stat().st_size
            layer_entries.append({
                "digest": new_digest,
                "size": size,
                "createdBy": f"RUN {arg}",
            })
            continue

    # ── Assemble manifest ─────────────────────────────────────────────────────
    if all_hits and not no_cache and existing_manifest:
        created = existing_manifest["created"]
    else:
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    manifest: dict = {
        "name": name,
        "tag": tag,
        "digest": "",
        "created": created,
        "config": {
            "Env": [f"{k}={v}" for k, v in sorted(env.items())],
            "Cmd": cmd or [],
            "WorkingDir": workdir,
        },
        "layers": layer_entries,
    }
    manifest["digest"] = store.compute_manifest_digest(manifest)
    store.save_manifest(manifest)

    short = manifest["digest"][7:15]
    print(f"\nSuccessfully built {manifest['digest'][:19]} {name}:{tag} ({total_time:.2f}s)")
    return manifest


# ── private helpers ───────────────────────────────────────────────────────────

def _parse_image_ref(arg: str) -> tuple[str, str]:
    if ":" in arg:
        name, tag = arg.rsplit(":", 1)
    else:
        name, tag = arg, "latest"
    return name.strip(), tag.strip()


def _parse_env_list(env_list: list[str]) -> list[tuple[str, str]]:
    result = []
    for item in env_list:
        k, _, v = item.partition("=")
        result.append((k, v))
    return result


def _parse_copy_arg(arg: str) -> tuple[str, str]:
    """Split COPY arg into (src_pattern, dest)."""
    parts = arg.split()
    if len(parts) < 2:
        raise ValueError(f"COPY requires <src> <dest>, got: {arg}")
    # last token is dest; all others are sources (we support one glob pattern)
    dest = parts[-1]
    src = " ".join(parts[:-1])
    return src, dest


def _expand_copy_sources(
    context: Path,
    src_pattern: str,
    dest: str,
) -> tuple[list[tuple[str, Path]], list[tuple[str, str]]]:
    """
    Expand *src_pattern* relative to *context*.
    Returns (file_pairs, file_hashes) where:
      file_pairs  – [(archive_path, host_path), ...]
      file_hashes – [(rel_src_path, sha256_hex), ...] for cache key
    """
    import glob as _glob

    dest_clean = dest.lstrip("/")

    # Expand glob
    if src_pattern == ".":
        matched = sorted(context.rglob("*"))
    else:
        matched = sorted(
            Path(p) for p in _glob.glob(str(context / src_pattern), recursive=True)
        )

    if not matched:
        raise ValueError(f"COPY: no files matched pattern '{src_pattern}'")

    file_pairs: list[tuple[str, Path]] = []
    file_hashes: list[tuple[str, str]] = []

    for hp in matched:
        rel = hp.relative_to(context)
        rel_str = str(rel)

        if hp.is_symlink() or hp.is_file():
            # If dest ends with / or has >1 source, dest is directory prefix
            if dest.endswith("/") or len(matched) > 1 or src_pattern == ".":
                arc = (dest_clean + "/" + rel_str).lstrip("/")
            else:
                arc = dest_clean
            file_pairs.append((arc, hp))

            if hp.is_file():
                h = hashlib.sha256(hp.read_bytes()).hexdigest()
                file_hashes.append((rel_str, h))

        elif hp.is_dir():
            if dest.endswith("/") or src_pattern == ".":
                arc = (dest_clean + "/" + rel_str).lstrip("/")
            else:
                arc = dest_clean
            # recurse into directory
            for sub in sorted(hp.rglob("*")):
                sub_rel = str(sub.relative_to(context))
                if dest.endswith("/") or src_pattern == ".":
                    sub_arc = (dest_clean + "/" + sub_rel).lstrip("/")
                else:
                    sub_arc = (dest_clean + "/" + str(sub.relative_to(hp))).lstrip("/")
                file_pairs.append((sub_arc, sub))
                if sub.is_file():
                    h = hashlib.sha256(sub.read_bytes()).hexdigest()
                    file_hashes.append((sub_rel, h))

    return file_pairs, file_hashes


def _assemble_rootfs(layer_entries: list[dict], rootfs: Path) -> None:
    """Extract all layers in order into *rootfs*."""
    for entry in layer_entries:
        layers.extract_layer(entry["digest"], store.LAYERS_DIR, rootfs)


def _execute_run(
    shell_cmd: str,
    layer_entries: list[dict],
    env: dict[str, str],
    workdir: str,
) -> str:
    """
    Execute *shell_cmd* inside an isolated rootfs assembled from *layer_entries*.
    Captures the filesystem delta and stores it as a new layer.
    Returns the new layer digest.
    """
    with tempfile.TemporaryDirectory(prefix="docksmith_run_") as tmpdir:
        rootfs = Path(tmpdir) / "rootfs"
        rootfs.mkdir()

        # Assemble filesystem from all previous layers
        _assemble_rootfs(layer_entries, rootfs)

        # Silent WORKDIR creation (not part of the delta)
        if workdir:
            wd_host = rootfs / workdir.lstrip("/")
            wd_host.mkdir(parents=True, exist_ok=True)

        # Snapshot before state
        before = layers.scan_tree(rootfs)

        # Run command in isolation
        rc = run_isolated(
            rootfs,
            ["/bin/sh", "-c", shell_cmd],
            env,
            workdir,
        )
        if rc != 0:
            raise RuntimeError(
                f"RUN command failed with exit code {rc}: {shell_cmd}"
            )

        # Build delta tar from changes
        tar_bytes = layers.make_run_delta_tar(rootfs, before)
        return store.write_layer(tar_bytes)
