# Docksmith – Design & Implementation Report

## 1. Overview

Docksmith is a simplified Docker-like build and runtime system implemented from scratch in pure Python (stdlib only, no external packages). It runs as a single CLI binary with all state stored in `~/.docksmith/`. No daemon is required.

---

## 2. Architecture

```
cc/
├── docksmith/
│   ├── cli.py        – argparse entry-point, dispatches subcommands
│   ├── store.py      – ~/.docksmith/ state read/write (manifests, layers, cache)
│   ├── parser.py     – Docksmithfile parser
│   ├── layers.py     – tar creation (COPY delta, RUN delta), tar extraction
│   ├── cache.py      – cache-key computation + index lookup/record
│   ├── runtime.py    – Linux process isolation (unshare + chroot)
│   ├── builder.py    – main build-loop (drives all six instructions)
│   └── images.py     – list / remove images
├── sample/
│   ├── Docksmithfile – sample app using all 6 instructions
│   └── run.sh        – the container's CMD script
├── scripts/
│   └── setup_images.py – one-time Alpine 3.18 base-image download + import
├── run.sh            – demo script (all 9 demo steps)
├── pyproject.toml
└── REPORT.md
```

---

## 3. State Directory Layout

```
~/.docksmith/
  images/       ← one JSON manifest per image  (<name>_<tag>.json)
  layers/       ← content-addressed raw tar files  (sha256_<hex>)
  cache/
    index.json  ← maps cache_key_hex → layer_digest
```

---

## 4. Build Instructions

All six required instructions are implemented in `builder.py`:

| Instruction | Layer? | Behaviour |
|-------------|--------|-----------|
| `FROM` | No | Loads base manifest, seeds `layer_entries`, `env`, `workdir`. Prev-digest for first cache key = base manifest digest. |
| `WORKDIR` | No | Updates `workdir` state variable. Silently creates the dir in the assembled rootfs before next layer-producing step (not stored in delta). |
| `ENV` | No | Updates `env` dict. Injected into every RUN command and every container. |
| `CMD` | No | Stores `cmd` list (JSON-array form required). |
| `COPY` | **Yes** | Expands glob in context, builds sorted+zeroed delta tar from matched files. |
| `RUN` | **Yes** | Assembles rootfs, snapshots before, runs in isolation, diffs after, builds delta tar. |

Any unrecognised instruction raises `ValueError` with line number and aborts immediately.

---

## 5. Image Format

### 5.1 Manifest (`~/.docksmith/images/<name>_<tag>.json`)

```json
{
  "name": "myapp",
  "tag": "latest",
  "digest": "sha256:<hash>",
  "created": "2026-04-15T12:00:00Z",
  "config": {
    "Env": ["APP_NAME=DocksmithApp", "APP_VERSION=1.0.0"],
    "Cmd": ["sh", "/app/run.sh"],
    "WorkingDir": "/app"
  },
  "layers": [
    { "digest": "sha256:aaa...", "size": 2816000, "createdBy": "FROM alpine:3.18 (minirootfs import)" },
    { "digest": "sha256:bbb...", "size": 4096,    "createdBy": "COPY . /app" },
    { "digest": "sha256:ccc...", "size": 512,     "createdBy": "RUN sh -c ..." }
  ]
}
```

### 5.2 Manifest Digest

Computed per spec: JSON-serialise the manifest with `digest=""`, SHA-256 those bytes, write back as `"sha256:<hex>"`. Key insertion order is preserved (Python 3.7+ dict), ensuring a stable canonical form.

### 5.3 Layers

Each `COPY` or `RUN` instruction produces a **delta tar** (only added/changed files, not a full snapshot). Layers are stored uncompressed as raw tar bytes named by `sha256_<hex>`. All entries are written with:
- sorted by archive path (ascending lexicographic)
- `mtime = 0`
- `uid = gid = 0`, `uname = gname = ""`

This guarantees identical byte output—and identical digest—for the same logical content on any build run.

---

## 6. Build Cache

### 6.1 Cache Key

Computed in `cache.py` using `hashlib.sha256` of a newline-joined string:

```
prev_layer_digest
instruction_text      (e.g. "COPY . /app")
workdir               (empty string if unset)
env_state             (sorted KEY=value lines, empty if none)
[COPY only] sorted SHA-256 hex of each source file, one per line
```

- For the **first** layer-producing instruction: `prev_layer_digest = base_manifest["digest"]`.  
  Changing `FROM` therefore invalidates all downstream cache entries.
- For subsequent instructions: `prev_digest = digest of last COPY/RUN layer`.

### 6.2 Cache Hit / Miss Logic

```
CACHE HIT  ← key present in index AND layer file exists on disk
CACHE MISS ← either condition fails
```

Once a miss occurs, **all subsequent steps are forced misses** (cascade bit flips in the builder).

`--no-cache` skips all lookups and writes; layers are still stored normally.

### 6.3 `created` Timestamp Preservation

On a fully warm rebuild (all hits), the builder reuses the existing manifest's `created` timestamp. Because all layer digests are identical (cache hits return the same digests), the manifest JSON is byte-for-byte identical, producing the same manifest digest. This satisfies "reproducible builds across same-machine rebuilds."

---

## 7. Container Runtime

### 7.1 Isolation Mechanism

The **same function** (`runtime.run_isolated`) is used for both `RUN` during build and `docksmith run`. It invokes:

```
unshare --user --map-root-user --mount --pid --fork
  chroot <rootfs>
    /bin/sh -c "
      mount -t proc proc /proc 2>/dev/null || true;
      export KEY=val;   # ... all ENV vars
      cd /workdir;
      exec <cmd>
    "
```

- `--user --map-root-user`: Creates a new user namespace and maps the calling user to UID 0 inside it, granting `CAP_SYS_CHROOT` within the namespace without requiring host root.
- `--mount`: New mount namespace—`/proc` mounting and any future bind-mounts are invisible to the host.
- `--pid --fork`: New PID namespace; the child becomes PID 1.
- `chroot <rootfs>`: Changes the process root to the assembled layer directory. Any path the container process accesses is resolved relative to this directory, not the host root.

### 7.2 Isolation Guarantee

A file written at `/tmp/x` inside the container actually appears at `<tmpdir>/tmp/x` on the host. The container cannot reach outside `<tmpdir>`. After exit, the temp directory is deleted entirely, leaving no trace on the host filesystem.

### 7.3 Filesystem Assembly

Before running either `RUN` or `docksmith run`, all layers are extracted in order into a fresh temporary directory using `tarfile.extract`. Later layers overwrite earlier ones at the same path—identical to how overlay filesystems work conceptually.

### 7.4 RUN Delta Capture

1. Assemble all preceding layers into `rootfs/`.
2. Snapshot `before = scan_tree(rootfs)` — records hash, mode, symlink target for every entry.
3. Run the shell command in isolation **inside** `rootfs/` (mutates it in-place).
4. `make_run_delta_tar(rootfs, before)` diffs after-state vs before-state, collects new/changed entries, builds a sorted zero-timestamp delta tar.
5. Store the delta as a new layer.

---

## 8. CLI Reference

```
docksmith build -t <name:tag> [--no-cache] <context>
docksmith images
docksmith rmi   <name:tag>
docksmith run   [-e KEY=VAL ...] <name:tag> [cmd ...]
```

All errors produce a human-readable message to stderr and exit with code 1.

---

## 9. Base Image

Alpine Linux 3.18 minirootfs is downloaded once by `scripts/setup_images.py` from the official Alpine CDN. The script:

1. Downloads the .tar.gz (≈2.7 MB).
2. Decompresses and re-creates the tar with normalised paths, sorted entries, and zeroed timestamps for a deterministic digest.
3. Stores the layer and writes the `alpine:3.18` manifest.

After setup, **all operations are fully offline**. No network access occurs during build or run.

---

## 10. Reproducibility

| Factor | Handling |
|--------|----------|
| File order in tar | `sorted()` by archive path before writing |
| File timestamps | `mtime = 0` on every `TarInfo` |
| Ownership | `uid = gid = 0`, `uname = gname = ""` |
| ENV serialisation | Sorted by key, `KEY=value` lines joined by `\n` |
| Manifest JSON | Stable insertion-order dict; `json.dumps(indent=2)` |
| `created` field | Preserved from existing manifest on all-hit rebuild |

---

## 11. Security Notes

- **No host escape**: `chroot` into a temp directory ensures the container process cannot access host paths. The Linux user namespace grants container-root without host root.
- **Path sanitisation**: During tar extraction, entries with leading `/` or `..` components are cleaned or skipped to prevent path traversal attacks.
- **No privilege escalation**: The implementation uses only existing Linux user-namespace features available to unprivileged users on kernels ≥ 3.12.

---

## 12. Known Limitations (Out of Scope per Spec)

- No whiteout support (file deletions inside RUN not recorded in delta).
- No networking, resource limits, bind mounts, or detached mode.
- No multi-stage builds, ENTRYPOINT, ARG, EXPOSE, VOLUME, ADD, or SHELL.
- macOS / Windows not supported (Linux-only isolation primitive).
- No garbage collection of unreferenced layers.

---

## 13. Demo Steps

| # | Command | Expected Result |
|---|---------|-----------------|
| 1 | `docksmith build --no-cache -t myapp:latest ./sample` | All CACHE MISS, full build |
| 2 | `docksmith build -t myapp:latest ./sample` | All CACHE HIT, near-instant |
| 3 | Edit `sample/run.sh`, rebuild | COPY + RUN show CACHE MISS |
| 4 | `docksmith images` | Lists myapp:latest with digest prefix and timestamp |
| 5 | `docksmith run myapp:latest` | Container starts, prints greeting, exits 0 |
| 6 | `docksmith run -e APP_NAME=Overridden myapp:latest` | Overridden value appears in output |
| 7 | Write `/tmp/isolation_test.txt` inside, check host | File absent on host — **PASS** |
| 8 | `docksmith rmi myapp:latest` | Manifest + layers deleted |
