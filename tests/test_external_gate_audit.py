"""Independent regressions for audit A01-A03; synthetic evidence only."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant_ai.operations.pilot_gate import (
    EXTERNAL_GATES,
    evidence_bundle_digest,
    external_gate_report,
)


class ExternalGateAuditTests(unittest.TestCase):
    def test_explicit_flags_and_observation_clock(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proof").write_text("synthetic audit evidence")
            def document(flag=True, observed=None):
                return {
                    "revision": "a" * 40, "targetHost": "fixture",
                    "gates": {gate.gate_id: {
                        "status": "passed", "reviewer": "fixture",
                        "observedAt": (observed or now).isoformat(),
                        "evidence": ["proof"],
                        "evidenceSha256": evidence_bundle_digest(["proof"], root),
                        **{key: flag for key in gate.required_evidence},
                    } for gate in EXTERNAL_GATES},
                }
            for flag in (True, "passed"):
                with self.subTest(accepted=flag):
                    self.assertTrue(external_gate_report(document(flag), evidence_root=root, now=now)["ready"])
            for flag in (1, 1.0, 0, False, None, [], {}):
                with self.subTest(rejected=flag):
                    self.assertFalse(external_gate_report(document(flag), evidence_root=root, now=now)["ready"])
            for offset, expected in ((-86400, True), (0, True), (5, True), (6, False), (31536000, False)):
                with self.subTest(seconds=offset):
                    result = external_gate_report(document(observed=now + timedelta(seconds=offset)), evidence_root=root, now=now)
                    self.assertEqual(result["ready"], expected)
                    self.assertFalse(result["liveExecutionEnabled"])
            equivalent = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
            self.assertTrue(external_gate_report(document(observed=equivalent), evidence_root=root, now=now)["ready"])
            for timestamp in ("bad", "2026-09-15T00:00:00"):
                source = document()
                source["gates"]["X01"]["observedAt"] = timestamp
                self.assertFalse(external_gate_report(source, evidence_root=root, now=now)["ready"])
            with self.assertRaises(ValueError):
                external_gate_report(document(), evidence_root=root, now=now.replace(tzinfo=None))
