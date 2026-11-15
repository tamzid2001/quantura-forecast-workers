"""Restore reviewed public snapshots or frozen registration, never invent state.

The workflow pins the manifest hash. Every payload is bounded, source-hashed and
schema-validated before an atomic copy. This module performs no network writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

KEYS = ("stocks", "games-kalshi-0", "games-kalshi-1", "games-polymarket_us-0",
        "games-polymarket_us-1", "perps-0", "perps-1", "perps-2", "perps-3", "sports-research-registry")
MAX_MANIFEST = 64 * 1024
MAX_SNAPSHOT = 24 * 1024 * 1024
MAX_REGISTRY = 64 * 1024


def bounded(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("RECOVERY_SEED_TOO_LARGE")
    return value


def needs_restore(feed: str) -> bool:
    """An unavailable provider/API is not proof that durable state is absent."""
    if feed not in KEYS or feed == "sports-research-registry":
        raise ValueError("RECOVERY_FEED_INVALID")
    from .public_snapshots import previous
    try:
        previous(feed)
    except ValueError as error:
        if str(error) == "PUBLIC_SNAPSHOT_NOT_PUBLISHED":
            return True
        raise
    return False


def restore_seed(directory: Path, key: str, manifest_sha256: str, output: Path) -> dict:
    if key not in KEYS or not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise ValueError("RECOVERY_REQUEST_INVALID")
    raw_manifest = bounded(directory / "manifest.json", MAX_MANIFEST)
    if hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256:
        raise ValueError("RECOVERY_MANIFEST_HASH_MISMATCH")
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("schema") != "quantura-recovery-seeds-v1":
        raise ValueError("RECOVERY_MANIFEST_INVALID")
    entry = manifest.get("assets", {}).get(key)
    filename = key + (".json" if key == "sports-research-registry" else ".json.gz")
    if (not isinstance(entry, dict) or entry.get("file") != filename
            or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", "")))):
        raise ValueError("RECOVERY_ASSET_INVALID")
    source = directory / filename
    if source.is_symlink() or source.resolve().parent != directory.resolve():
        raise ValueError("RECOVERY_ASSET_PATH_INVALID")
    registry = key == "sports-research-registry"
    payload = bounded(source, MAX_REGISTRY if registry else MAX_SNAPSHOT)
    if (hashlib.sha256(payload).hexdigest() != entry["sha256"]
            or len(payload) != entry.get("bytes")):
        raise ValueError("RECOVERY_PAYLOAD_HASH_MISMATCH")
    if registry:
        from .sports_research.prospective import validate
        state = validate(json.loads(payload))
        as_of = state["created_at"]
        destination = output / "registry.json"
    else:
        from .public_snapshots import unpack
        state = unpack(payload, key)
        if key == "stocks" and not state["data"]["items"]:
            raise ValueError("RECOVERY_STOCK_SNAPSHOT_EMPTY")
        as_of = state["published_at"]
        destination = output / "snapshot.json.gz"
    output.mkdir(parents=True, exist_ok=True)
    if destination.exists() and bounded(destination, len(payload)) != payload:
        raise ValueError("RECOVERY_DESTINATION_CONFLICT")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return {"key": key, "sha256": entry["sha256"], "bytes": len(payload),
            "source_as_of": as_of, "restored": str(destination), "new_forecasts": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe-feed", choices=KEYS[:-1])
    mode.add_argument("--key", choices=KEYS)
    parser.add_argument("--seed-dir", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.probe_feed:
        missing = needs_restore(args.probe_feed)
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write("restore=" + str(missing).lower() + "\n")
        print(json.dumps({"feed": args.probe_feed, "restore_required": missing}))
    else:
        if not args.seed_dir or not args.manifest_sha256 or not args.output:
            parser.error("seed-dir, manifest-sha256 and output are required")
        print(json.dumps(restore_seed(args.seed_dir, args.key, args.manifest_sha256, args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
