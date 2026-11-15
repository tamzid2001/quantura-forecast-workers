import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from market_research import recovery_seed as recovery
from market_research.sports_research.prospective import initialize


def seed(tmp_path, key="stocks", value=None, mutate=None):
    directory = tmp_path / "seed"
    directory.mkdir(exist_ok=True)
    if value is None:
        value = initialize(1793116800) if key == "sports-research-registry" else {
            "schema_version": "quantura-public-screener-v1", "feed": key,
            "published_at": "2026-10-30T20:00:00Z", "data": {"items": [{"ticker": "MSFT"}]}}
    raw = json.dumps(value).encode()
    payload = raw if key == "sports-research-registry" else gzip.compress(raw, mtime=0)
    filename = key + (".json" if key == "sports-research-registry" else ".json.gz")
    (directory / filename).write_bytes(payload)
    entry = {"file": filename, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    manifest = {"schema": "quantura-recovery-seeds-v1", "assets": {key: entry}}
    if mutate:
        mutate(manifest)
    encoded = json.dumps(manifest).encode()
    (directory / "manifest.json").write_bytes(encoded)
    return directory, hashlib.sha256(encoded).hexdigest(), payload


@pytest.mark.parametrize("key", recovery.KEYS)
def test_restore_preserves_source_bytes_dates_and_original_registration(tmp_path, key):
    directory, digest, payload = seed(tmp_path, key)
    output = tmp_path / "output"
    result = recovery.restore_seed(directory, key, digest, output)
    assert Path(result["restored"]).read_bytes() == payload
    assert result["new_forecasts"] == 0
    assert result["source_as_of"].startswith("2026-10-")
    assert recovery.restore_seed(directory, key, digest, output) == result


@pytest.mark.parametrize("mutation,error", [
    (lambda x: x.update(schema="unknown"), "MANIFEST_INVALID"),
    (lambda x: x["assets"].clear(), "ASSET_INVALID"),
    (lambda x: x["assets"]["stocks"].update(file="../credentials"), "ASSET_INVALID"),
    (lambda x: x["assets"]["stocks"].update(sha256="not-a-hash"), "ASSET_INVALID"),
    (lambda x: x["assets"]["stocks"].update(bytes=0), "PAYLOAD_HASH_MISMATCH"),
])
def test_manifest_contracts_fail_closed(tmp_path, mutation, error):
    directory, digest, _ = seed(tmp_path, mutate=mutation)
    with pytest.raises(ValueError, match=error):
        recovery.restore_seed(directory, "stocks", digest, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_hashes_paths_limits_and_existing_state_are_not_bypassed(tmp_path, monkeypatch):
    directory, digest, payload = seed(tmp_path)
    for key, manifest_hash in [("../stocks", digest), ("stocks", "invalid")]:
        with pytest.raises(ValueError, match="REQUEST_INVALID"):
            recovery.restore_seed(directory, key, manifest_hash, tmp_path / "output")
    with pytest.raises(ValueError, match="MANIFEST_HASH_MISMATCH"):
        recovery.restore_seed(directory, "stocks", "0" * 64, tmp_path / "output")
    source = directory / "stocks.json.gz"
    source.write_bytes(payload + b"tampered")
    with pytest.raises(ValueError, match="PAYLOAD_HASH_MISMATCH"):
        recovery.restore_seed(directory, "stocks", digest, tmp_path / "output")
    source.unlink()
    other = tmp_path / "elsewhere"
    other.write_bytes(payload)
    source.symlink_to(other)
    with pytest.raises(ValueError, match="PATH_INVALID"):
        recovery.restore_seed(directory, "stocks", digest, tmp_path / "output")
    source.unlink()
    source.write_bytes(payload)
    with monkeypatch.context() as scope:
        scope.setattr(recovery, "MAX_SNAPSHOT", 10)
        with pytest.raises(ValueError, match="TOO_LARGE"):
            recovery.restore_seed(directory, "stocks", digest, tmp_path / "output")
    output = tmp_path / "output"
    output.mkdir()
    (output / "snapshot.json.gz").write_bytes(b"different")
    with pytest.raises(ValueError, match="DESTINATION_CONFLICT"):
        recovery.restore_seed(directory, "stocks", digest, output)


def test_payload_schema_and_frozen_research_window_must_validate(tmp_path):
    directory, digest, _ = seed(tmp_path, value={"wrong": True})
    with pytest.raises(ValueError, match="PUBLIC_SNAPSHOT_INVALID"):
        recovery.restore_seed(directory, "stocks", digest, tmp_path / "output")
    directory, digest, _ = seed(tmp_path, value={"schema_version": "quantura-public-screener-v1", "feed": "stocks", "published_at": "2026-10-30T20:00:00Z", "data": {"items": []}})
    with pytest.raises(ValueError, match="STOCK_SNAPSHOT_EMPTY"):
        recovery.restore_seed(directory, "stocks", digest, tmp_path / "output")
    state = initialize(1793116800)
    state["ends_at"] = state["starts_at"]
    directory, digest, _ = seed(tmp_path, "sports-research-registry", state)
    with pytest.raises(ValueError, match="FROZEN_RESEARCH_WINDOW"):
        recovery.restore_seed(directory, "sports-research-registry", digest, tmp_path / "output")


def test_probe_only_recovers_absence_and_does_not_mask_corruption_or_api_failure():
    with patch("market_research.public_snapshots.previous", return_value={"items": []}):
        assert recovery.needs_restore("perps-0") is False
    with patch("market_research.public_snapshots.previous", side_effect=ValueError("PUBLIC_SNAPSHOT_NOT_PUBLISHED")):
        assert recovery.needs_restore("stocks") is True
    for error in [ValueError("PUBLIC_ARTIFACT_DIGEST_INVALID"), RuntimeError("provider unavailable")]:
        with patch("market_research.public_snapshots.previous", side_effect=error):
            with pytest.raises(type(error)):
                recovery.needs_restore("stocks")
    with pytest.raises(ValueError, match="FEED_INVALID"):
        recovery.needs_restore("sports-research-registry")


def test_cli_probe_and_restore_use_the_same_validation(tmp_path, monkeypatch, capsys):
    status = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(status))
    with patch.object(recovery, "needs_restore", return_value=True):
        assert recovery.main(["--probe-feed", "stocks"]) == 0
    assert status.read_text() == "restore=true\n"
    directory, digest, _ = seed(tmp_path)
    assert recovery.main(["--key", "stocks", "--seed-dir", str(directory), "--manifest-sha256", digest, "--output", str(tmp_path / "output")]) == 0
    assert '"new_forecasts": 0' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        recovery.main(["--key", "stocks"])
