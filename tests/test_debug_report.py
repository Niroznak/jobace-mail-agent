"""Tests for debug_report.py's log capture and report writing -- no network calls."""
import json
import logging

from mail_agent import debug_report


class TestLogCapture:
    def test_captures_messages_emitted_inside_the_block(self):
        logger = logging.getLogger("mail_agent.pipeline.verify")
        with debug_report.LogCapture() as capture:
            logger.info("[VERIFY] 'Role @ Company' -> some reason")
            logger.warning("something else")
        assert capture.lines == ["[VERIFY] 'Role @ Company' -> some reason", "something else"]

    def test_does_not_capture_messages_outside_the_block(self):
        logger = logging.getLogger("mail_agent.pipeline.verify")
        with debug_report.LogCapture() as capture:
            pass
        logger.info("after the block")
        assert capture.lines == []

    def test_handler_is_removed_after_the_block(self):
        root = logging.getLogger()
        before = len(root.handlers)
        with debug_report.LogCapture():
            assert len(root.handlers) == before + 1
        assert len(root.handlers) == before


class TestWriteReport:
    def test_writes_valid_json(self, tmp_path):
        path = tmp_path / "report.json"
        records = [{"mail_id": "m1", "action": "verify_failed", "log": ["reason one"]}]
        debug_report.write_report(str(path), records)
        assert json.loads(path.read_text(encoding="utf-8")) == records

    def test_overwrites_previous_report(self, tmp_path):
        path = tmp_path / "report.json"
        debug_report.write_report(str(path), [{"a": 1}])
        debug_report.write_report(str(path), [{"b": 2}])
        assert json.loads(path.read_text(encoding="utf-8")) == [{"b": 2}]


class TestPrintSummary:
    def test_runs_without_error_on_empty_and_populated_input(self, capsys):
        debug_report.print_summary([])
        out = capsys.readouterr().out
        assert "No candidates" in out

        debug_report.print_summary([
            {"title": "Role A", "company": "Acme", "action": "verify_failed", "log": ["could not fetch"]},
            {"title": "Role B", "company": "Acme", "action": "inserted", "log": ["score=80 -> passed"]},
        ])
        out = capsys.readouterr().out
        assert "verify_failed" in out and "inserted" in out and "Role A" in out
