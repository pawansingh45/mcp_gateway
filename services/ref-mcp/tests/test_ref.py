import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from ref_mcp import clock, notes


def call(app, name, args):
    r = app.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})["result"]
    text = r["content"][0]["text"]
    return r, (json.loads(text) if not r.get("isError") else text)


class ClockTests(unittest.TestCase):
    def setUp(self):
        fixed = datetime(2026, 10, 24, 6, 30, tzinfo=timezone.utc)
        self.app = clock.build_app(now_fn=lambda tz: fixed.astimezone(tz))

    def test_current_time_in_zone(self):
        _, out = call(self.app, "current_time", {"timezone": "Asia/Kolkata"})
        self.assertEqual(out["date"], "2026-10-24")
        self.assertEqual(out["time"], "12:00:00")
        self.assertEqual(out["weekday"], "Saturday")

    def test_unknown_zone_is_tool_error(self):
        r, text = call(self.app, "current_time", {"timezone": "Mars/Base"})
        self.assertTrue(r["isError"])
        self.assertIn("IANA", text)

    def test_convert(self):
        _, out = call(self.app, "convert_time", {"timestamp": "2026-10-24T09:00:00", "from_timezone": "UTC",
                                                 "to_timezone": "Asia/Tokyo"})
        self.assertTrue(out["iso"].startswith("2026-10-24T18:00:00"))


class NotesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "n.json")
        self.app = notes.build_app(notes.NoteStore(self.path))

    def tearDown(self):
        self.dir.cleanup()

    def test_save_search_delete_roundtrip_and_persistence(self):
        _, note = call(self.app, "save_note", {"title": "Orders", "body": "3 orders today"})
        _, found = call(self.app, "search_notes", {"query": "ORDERS"})
        self.assertEqual(found["count"], 1)
        # a brand new app on the same file sees the note: state really persisted
        again = notes.build_app(notes.NoteStore(self.path))
        _, listed = call(again, "list_notes", {})
        self.assertEqual(listed["notes"][0]["id"], note["id"])
        call(again, "delete_note", {"id": note["id"]})
        _, listed = call(again, "list_notes", {})
        self.assertEqual(listed["count"], 0)

    def test_delete_missing_and_empty_title(self):
        r, _ = call(self.app, "delete_note", {"id": "nope"})
        self.assertTrue(r["isError"])
        r, _ = call(self.app, "save_note", {"title": "  ", "body": "x"})
        self.assertTrue(r["isError"])

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        _, listed = call(self.app, "list_notes", {})
        self.assertEqual(listed["count"], 0)


if __name__ == "__main__":
    unittest.main()
