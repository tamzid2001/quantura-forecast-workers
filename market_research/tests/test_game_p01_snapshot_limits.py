"""Separate bounded forecast expansion from smaller durable state limits."""
import gzip
import io
import json
import unittest
from unittest.mock import patch
import zipfile

from market_research import game_p01_paper as paper


def archive(member, raw):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as result:
        result.writestr(member, gzip.compress(raw))
    return output.getvalue()


class SnapshotBoundsTests(unittest.TestCase):
    def test_production_limits_separate_expanded_forecasts_from_checkpoints(self):
        self.assertEqual(paper.MAX_SNAPSHOT_BYTES, 128 * 1024 * 1024)
        self.assertEqual(paper.MAX_BYTES, 32 * 1024 * 1024)

    def test_forecast_over_checkpoint_limit_is_accepted(self):
        value = {"payload": "x" * 180}
        packed = archive("snapshot.json.gz", json.dumps(value).encode())
        with patch.object(paper, "MAX_BYTES", 128), patch.object(paper, "MAX_SNAPSHOT_BYTES", 256):
            self.assertEqual(paper.compressed_json(packed, "snapshot.json.gz"), value)

    def test_same_payload_is_still_rejected_for_checkpoint(self):
        packed = archive("paper-state.json.gz", json.dumps({"payload": "x" * 180}).encode())
        with patch.object(paper, "MAX_BYTES", 128), patch.object(paper, "MAX_SNAPSHOT_BYTES", 256):
            with self.assertRaisesRegex(ValueError, "DECOMPRESSED_ARTIFACT_TOO_LARGE"):
                paper.compressed_json(packed, "paper-state.json.gz")

    def test_forecast_expansion_remains_bounded(self):
        packed = archive("snapshot.json.gz", json.dumps({"payload": "x" * 300}).encode())
        with patch.object(paper, "MAX_BYTES", 128), patch.object(paper, "MAX_SNAPSHOT_BYTES", 256):
            with self.assertRaisesRegex(ValueError, "DECOMPRESSED_ARTIFACT_TOO_LARGE"):
                paper.compressed_json(packed, "snapshot.json.gz")

    def test_exact_expanded_boundary_succeeds_but_next_byte_does_not(self):
        member = "snapshot.json.gz"
        with patch.object(paper, "MAX_BYTES", 128), patch.object(paper, "MAX_SNAPSHOT_BYTES", 256):
            raw = json.dumps("x" * 254).encode()
            self.assertEqual(len(raw), 256)
            self.assertEqual(paper.compressed_json(archive(member, raw), member), "x" * 254)
            with self.assertRaisesRegex(ValueError, "DECOMPRESSED_ARTIFACT_TOO_LARGE"):
                paper.compressed_json(archive(member, raw + b" "), member)

    def test_unexpected_archive_member_is_rejected(self):
        packed = archive("../snapshot.json.gz", b"{}")
        with self.assertRaisesRegex(ValueError, "INVALID_ARTIFACT_MEMBERS"):
            paper.compressed_json(packed, "snapshot.json.gz")


if __name__ == "__main__":
    unittest.main()
