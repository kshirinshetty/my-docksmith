"""
Build cache.

The cache index maps  cache_key (hex) → layer_digest ("sha256:<hex>").
Stored at ~/.docksmith/cache/index.json.

Cache key components (joined by newline, then SHA-256'd):
  1. prev_layer_digest   – digest of the last COPY/RUN layer, OR the base
                           image manifest digest for the first layer-producing
                           instruction.
  2. instruction_text    – full raw text of the instruction as written.
  3. workdir             – current WORKDIR value (empty string if unset).
  4. env_state           – KEY=value pairs sorted by key, one per line
                           (empty string if no ENV set yet).
  5. [COPY only] file_hashes – sha256 hex of each source file's bytes,
                               sorted by relative path, one per line.
"""

import hashlib

from . import store


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def compute_key(
    *,
    prev_digest: str,
    instruction_text: str,
    workdir: str,
    env: dict[str, str],
    file_hashes: list[tuple[str, str]] | None = None,
) -> str:
    """Return the hex cache key for a COPY or RUN instruction."""
    env_state = "\n".join(
        f"{k}={v}" for k, v in sorted(env.items())
    )

    parts = [
        prev_digest,
        instruction_text,
        workdir,
        env_state,
    ]

    if file_hashes is not None:
        # sorted by relative path
        fh_str = "\n".join(h for _, h in sorted(file_hashes, key=lambda x: x[0]))
        parts.append(fh_str)

    return _sha256("\n".join(parts))


def lookup(key: str, index: dict) -> str | None:
    """
    Return stored layer digest for *key* if it exists AND the layer file is
    present on disk.  Returns None on any miss.
    """
    digest = index.get(key)
    if digest is None:
        return None
    if not store.layer_exists(digest):
        return None
    return digest


def record(key: str, digest: str, index: dict) -> None:
    """Update the in-memory index with a new key→digest mapping."""
    index[key] = digest
