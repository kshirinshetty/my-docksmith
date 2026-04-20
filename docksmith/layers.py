"""
Layer utilities: create tar archives and extract them.

Rules (for reproducible digests):
  • Tar entries are added in lexicographically sorted order by archive path.
  • mtime = 0 on every entry.
  • uid = gid = 0, uname = gname = "" on every entry.
  • Layers are stored as *uncompressed* raw tar bytes.
  • The layer digest is sha256 of those raw bytes.
"""

import hashlib
import io
import os
import tarfile
from pathlib import Path


# ── tar helpers ───────────────────────────────────────────────────────────────

def _zero(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Zero out all non-reproducible fields on a TarInfo."""
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


# ── COPY layer ────────────────────────────────────────────────────────────────

def make_copy_tar(file_pairs: list[tuple[str, Path]]) -> bytes:
    """
    Build a delta tar for a COPY instruction.

    file_pairs: list of (archive_path, host_path)
      archive_path – path inside the tar (no leading slash), e.g. "app/main.sh"
      host_path    – absolute path on the host to read from

    Directories are inferred and added automatically.
    All entries are sorted; timestamps and ownership are zeroed.
    """
    # collect explicit directory entries needed
    dir_set: set[str] = set()
    for arc, hp in file_pairs:
        parts = arc.split("/")
        for depth in range(1, len(parts)):
            dir_set.add("/".join(parts[:depth]))
        if hp.is_dir():
            dir_set.add(arc)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # directories first (sorted)
        for dpath in sorted(dir_set):
            info = _zero(tarfile.TarInfo(name=dpath))
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)

        # files (sorted by archive path)
        for arc, hp in sorted(file_pairs, key=lambda x: x[0]):
            if hp.is_symlink():
                info = _zero(tarfile.TarInfo(name=arc))
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(hp)
                info.mode = 0o777
                tar.addfile(info)
            elif hp.is_dir():
                pass  # already handled above
            elif hp.is_file():
                data = hp.read_bytes()
                info = _zero(tarfile.TarInfo(name=arc))
                info.size = len(data)
                info.mode = hp.stat().st_mode & 0o7777
                tar.addfile(info, io.BytesIO(data))

    return buf.getvalue()


# ── RUN delta layer ───────────────────────────────────────────────────────────

def scan_tree(root: Path) -> dict[str, dict]:
    """
    Recursively scan *root* and return a mapping:
      relative_path → { "type": "file"|"dir"|"symlink",
                        "hash": sha256hex | None,
                        "mode": int,
                        "target": str | None }
    Symlinks are recorded by target, not followed.
    """
    result: dict[str, dict] = {}

    def _walk(cur: Path) -> None:
        try:
            entries = sorted(os.scandir(str(cur)), key=lambda e: e.name)
        except PermissionError:
            return
        for entry in entries:
            rel = str(Path(entry.path).relative_to(root))
            if entry.is_symlink():
                result[rel] = {
                    "type": "symlink",
                    "hash": None,
                    "mode": 0o777,
                    "target": os.readlink(entry.path),
                }
            elif entry.is_dir(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                result[rel] = {
                    "type": "dir",
                    "hash": None,
                    "mode": st.st_mode & 0o7777,
                    "target": None,
                }
                _walk(Path(entry.path))
            else:
                try:
                    data = Path(entry.path).read_bytes()
                    h = hashlib.sha256(data).hexdigest()
                except (PermissionError, OSError):
                    h = ""
                st = entry.stat(follow_symlinks=False)
                result[rel] = {
                    "type": "file",
                    "hash": h,
                    "mode": st.st_mode & 0o7777,
                    "target": None,
                }

    _walk(root)
    return result


def make_run_delta_tar(rootfs: Path, before: dict[str, dict]) -> bytes:
    """
    Compare *rootfs* (after a RUN command mutated it) against *before* snapshot.
    Returns a delta tar of all new or changed files/symlinks.
    Deleted files are NOT whiteout-ed (out of scope for this project).
    """
    after = scan_tree(rootfs)

    changed: list[str] = []
    for rel, meta in after.items():
        if rel not in before:
            changed.append(rel)
        elif meta["type"] == "file" and meta["hash"] != before[rel].get("hash"):
            changed.append(rel)
        elif meta["type"] == "symlink" and meta["target"] != before[rel].get("target"):
            changed.append(rel)
        # dirs: only add if new
        elif meta["type"] == "dir" and rel not in before:
            changed.append(rel)

    # collect directories implied by changed file paths
    dir_set: set[str] = set()
    for rel in changed:
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            candidate = "/".join(parts[:depth])
            if candidate not in before:
                dir_set.add(candidate)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # directories
        for dpath in sorted(dir_set):
            info = _zero(tarfile.TarInfo(name=dpath))
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)

        # changed entries (sorted)
        for rel in sorted(changed):
            meta = after[rel]
            full = rootfs / rel
            if meta["type"] == "symlink":
                info = _zero(tarfile.TarInfo(name=rel))
                info.type = tarfile.SYMTYPE
                info.linkname = meta["target"]
                info.mode = 0o777
                tar.addfile(info)
            elif meta["type"] == "dir":
                info = _zero(tarfile.TarInfo(name=rel))
                info.type = tarfile.DIRTYPE
                info.mode = meta["mode"]
                tar.addfile(info)
            elif meta["type"] == "file":
                try:
                    data = full.read_bytes()
                except (PermissionError, OSError):
                    continue
                info = _zero(tarfile.TarInfo(name=rel))
                info.size = len(data)
                info.mode = meta["mode"]
                tar.addfile(info, io.BytesIO(data))

    return buf.getvalue()


# ── extraction ────────────────────────────────────────────────────────────────

def extract_layer(digest: str, layers_dir: Path, dest: Path) -> None:
    """Extract a stored layer tar into *dest*, sanitising paths."""
    lf = layers_dir / digest.replace(":", "_")
    if not lf.exists():
        raise FileNotFoundError(f"Layer {digest} not found in {layers_dir}")

    with tarfile.open(str(lf), "r") as tar:
        for member in tar.getmembers():
            # strip leading slashes / dots
            member.name = member.name.lstrip("./")
            if not member.name:
                continue
            # reject path traversal
            parts = Path(member.name).parts
            if ".." in parts:
                continue
            try:
                # set_attrs=True → Python tarfile applies chmod (but skips
                # chown when not root).  This preserves execute bits.
                tar.extract(member, str(dest))
            except Exception:
                pass  # skip unextractable entries (e.g. device nodes without root)
