"""
State management for ~/.docksmith/

Layout:
  ~/.docksmith/
    images/   – one JSON manifest per image (<name>_<tag>.json)
    layers/   – content-addressed raw tar files  (sha256_<hex>)
    cache/    – index.json  (cache_key -> layer_digest)
"""

import hashlib
import json
from pathlib import Path

DOCKSMITH_DIR = Path.home() / ".docksmith"
IMAGES_DIR = DOCKSMITH_DIR / "images"
LAYERS_DIR = DOCKSMITH_DIR / "layers"
CACHE_DIR = DOCKSMITH_DIR / "cache"
CACHE_INDEX_FILE = CACHE_DIR / "index.json"


def init_store() -> None:
    for d in (IMAGES_DIR, LAYERS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── image name helpers ────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    """Make a name/tag safe for use in a filename."""
    return s.replace("/", "__").replace(":", "__")


def image_path(name: str, tag: str) -> Path:
    return IMAGES_DIR / f"{_safe(name)}_{_safe(tag)}.json"


# ── manifest I/O ──────────────────────────────────────────────────────────────

def load_manifest(name: str, tag: str) -> dict | None:
    p = image_path(name, tag)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def save_manifest(manifest: dict) -> None:
    p = image_path(manifest["name"], manifest["tag"])
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)
    return p


def list_all_manifests() -> list[dict]:
    if not IMAGES_DIR.exists():
        return []
    result = []
    for p in sorted(IMAGES_DIR.glob("*.json")):
        with open(p) as f:
            result.append(json.load(f))
    return result


# ── layer I/O ─────────────────────────────────────────────────────────────────

def layer_file(digest: str) -> Path:
    """Return the path to a layer's raw tar file given its digest string."""
    # digest = "sha256:<hex>"  →  file = layers/sha256_<hex>
    clean = digest.replace(":", "_")
    return LAYERS_DIR / clean


def layer_exists(digest: str) -> bool:
    return layer_file(digest).exists()


def write_layer(data: bytes) -> str:
    """Write raw tar bytes to the layers store.  Returns the digest."""
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    dest = layer_file(digest)
    if not dest.exists():
        dest.write_bytes(data)
    return digest


# ── cache I/O ─────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if not CACHE_INDEX_FILE.exists():
        return {}
    with open(CACHE_INDEX_FILE) as f:
        return json.load(f)


def save_cache(index: dict) -> None:
    with open(CACHE_INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2)


# ── manifest digest ───────────────────────────────────────────────────────────

def compute_manifest_digest(manifest: dict) -> str:
    """
    Per spec: serialize manifest with digest="" → SHA-256 → "sha256:<hex>".
    Key order must be stable; we preserve insertion order (Python 3.7+).
    """
    m = {k: ("" if k == "digest" else v) for k, v in manifest.items()}
    canonical = json.dumps(m, indent=2)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
