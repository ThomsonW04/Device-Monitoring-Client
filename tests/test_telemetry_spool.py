"""Regression coverage for bounded telemetry-spool recovery."""

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


AGENT_PATH = Path(__file__).resolve().parents[1] / "telemetry_agent.py"
SPEC = importlib.util.spec_from_file_location("telemetry_agent_spool_under_test", AGENT_PATH)
assert SPEC is not None and SPEC.loader is not None
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class TelemetrySpoolTests(unittest.TestCase):
    @staticmethod
    def sample(index: int) -> dict[str, int]:
        return {"sample": index}

    def test_new_samples_rotate_into_small_uploadable_segments(self) -> None:
        with TemporaryDirectory() as directory:
            spool = Path(directory) / "telemetry-spool.jsonl"
            original_limit = agent.SPOOL_SEGMENT_MAX_BYTES
            try:
                agent.SPOOL_SEGMENT_MAX_BYTES = 24
                agent.append_sample(spool, self.sample(1), 10)
                agent.append_sample(spool, self.sample(2), 10)
                agent.rotate_active_spool(spool)
                segments = agent.spool_segments(spool)
                self.assertEqual(len(segments), 2)
                self.assertEqual([agent.read_segment(segment) for segment in segments], [[self.sample(1)], [self.sample(2)]])
            finally:
                agent.SPOOL_SEGMENT_MAX_BYTES = original_limit

    def test_legacy_spool_advances_an_offset_without_rewriting_it(self) -> None:
        with TemporaryDirectory() as directory:
            spool = Path(directory) / "telemetry-spool.jsonl"
            spool.write_text("".join(json.dumps(self.sample(index)) + "\n" for index in range(5)), encoding="utf-8")
            original_batch = agent.MAX_BATCH_SIZE
            try:
                agent.MAX_BATCH_SIZE = 2
                samples, offset, file_size = agent.read_legacy_batch(spool, 0)
                self.assertEqual(samples, [self.sample(0), self.sample(1)])
                self.assertLess(offset, file_size)
                agent.save_legacy_offset(spool, offset)
                self.assertEqual(agent.load_legacy_offset(spool), offset)
                self.assertEqual(spool.read_text(encoding="utf-8").count("\n"), 5)
            finally:
                agent.MAX_BATCH_SIZE = original_batch

    def test_upload_limits_legacy_catchup_work_per_wakeup(self) -> None:
        with TemporaryDirectory() as directory:
            spool = Path(directory) / "telemetry-spool.jsonl"
            spool.write_text("".join(json.dumps(self.sample(index)) + "\n" for index in range(6)), encoding="utf-8")
            original_batch = agent.MAX_BATCH_SIZE
            original_limit = agent.MAX_CATCHUP_BATCHES_PER_UPLOAD
            original_sender = agent.send_telemetry_batch
            sent: list[list[dict[str, int]]] = []
            try:
                agent.MAX_BATCH_SIZE = 2
                agent.MAX_CATCHUP_BATCHES_PER_UPLOAD = 2
                agent.send_telemetry_batch = lambda _config, samples: sent.append(samples) or True
                self.assertTrue(agent.upload({"SPOOL_PATH": str(spool)}))
                self.assertEqual(sent, [[self.sample(0), self.sample(1)], [self.sample(2), self.sample(3)]])
                self.assertTrue(spool.exists())
                self.assertGreater(agent.load_legacy_offset(spool), 0)
            finally:
                agent.MAX_BATCH_SIZE = original_batch
                agent.MAX_CATCHUP_BATCHES_PER_UPLOAD = original_limit
                agent.send_telemetry_batch = original_sender


if __name__ == "__main__":
    unittest.main()
