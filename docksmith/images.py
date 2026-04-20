"""
Image management helpers used by the CLI (images, rmi).
"""

from pathlib import Path

from . import store


def list_images() -> None:
    """Print all images in columnar format."""
    manifests = store.list_all_manifests()
    if not manifests:
        print("No images found.")
        return

    hdr = f"{'NAME':<20} {'TAG':<20} {'ID':<14} {'CREATED'}"
    print(hdr)
    print("-" * len(hdr))
    for m in manifests:
        digest = m.get("digest", "")
        short_id = digest.replace("sha256:", "")[:12] if digest else "unknown"
        print(
            f"{m.get('name', '?'):<20} "
            f"{m.get('tag', '?'):<20} "
            f"{short_id:<14} "
            f"{m.get('created', '?')}"
        )


def remove_image(name: str, tag: str) -> None:
    """
    Remove the manifest and ALL layer files listed in it.

    NOTE: no reference counting – if another image shares a layer, that
    layer file will be deleted and that image will be broken.  This is
    expected per spec.
    """
    manifest = store.load_manifest(name, tag)
    if manifest is None:
        raise RuntimeError(f"Image '{name}:{tag}' not found.")

    removed_layers: list[str] = []
    for entry in manifest.get("layers", []):
        lf = store.layer_file(entry["digest"])
        if lf.exists():
            lf.unlink()
            removed_layers.append(entry["digest"])

    # Remove manifest file
    mp = store.image_path(name, tag)
    if mp.exists():
        mp.unlink()

    print(f"Untagged: {name}:{tag}")
    for d in removed_layers:
        print(f"Deleted layer: {d[:19]}")
    print(f"Removed image '{name}:{tag}'")
