"""Privacy regression tests for diagnostic snapshot collection."""

import importlib.util
import inspect
from io import StringIO
from pathlib import Path
import unittest


AGENT_PATH = Path(__file__).resolve().parents[1] / "telemetry_agent.py"
SPEC = importlib.util.spec_from_file_location("telemetry_agent_under_test", AGENT_PATH)
assert SPEC is not None and SPEC.loader is not None
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class SnapshotPrivacyTests(unittest.TestCase):
    def test_common_secret_forms_are_redacted(self) -> None:
        secret = "do-not-store-this"
        content = (
            f"password={secret}\n"
            f"Authorization: Bearer {secret}\n"
            f"postgres://monitor:{secret}@database/telemetry\n"
        )

        redacted = agent.redact_sensitive_values(content)

        self.assertNotIn(secret, redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 3)

    def test_snapshot_attachment_is_limited_by_utf8_bytes(self) -> None:
        content = "é" * agent.MAX_SNAPSHOT_TRANSPORT_BYTES

        bounded = agent.bounded_snapshot_content(content)

        self.assertLessEqual(
            len(bounded.encode("utf-8")), agent.MAX_SNAPSHOT_TRANSPORT_BYTES
        )
        self.assertTrue(bounded.endswith(agent.SNAPSHOT_TRUNCATION_NOTICE))

    def test_written_sections_redact_before_local_log_storage(self) -> None:
        log_file = StringIO()
        agent.write_section(log_file, "TEST", "token=do-not-store-this")

        self.assertNotIn("do-not-store-this", log_file.getvalue())
        self.assertIn("[REDACTED]", log_file.getvalue())

    def test_snapshot_process_collection_excludes_command_arguments(self) -> None:
        process_source = inspect.getsource(agent.process_io_output)
        snapshot_source = inspect.getsource(agent.write_snapshot)

        self.assertNotIn(' / "cmdline"', process_source)
        self.assertIn(' / "comm"', process_source)
        self.assertNotIn("comm,args", snapshot_source)


if __name__ == "__main__":
    unittest.main()
