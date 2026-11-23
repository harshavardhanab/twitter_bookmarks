import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bm import api, db, search, translate


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"BM_HOME": home}))
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def insert(self, source_id, **values):
        item = {"source": "x", "source_id": source_id, "text": "An English bookmark."}
        db.upsert_item(self.conn, item | values)

    def test_recent_prefers_x_rank_to_import_time(self):
        for list_name in ("bookmark", "like"):
            with self.subTest(list_name=list_name):
                self.insert(f"{list_name}-1", list=list_name, list_rank="20", seen_at="2026-11-21")
                self.insert(f"{list_name}-2", list=list_name, list_rank="10", seen_at="2026-11-23")
                self.insert(f"{list_name}-3", list=list_name, seen_at="2026-11-24")
                results = api.recent(self.conn, in_list=list_name)
                self.assertEqual([r["id"] for r in results], [f"x:{list_name}-{n}" for n in (1, 2, 3)])
                self.assertEqual(results[0]["first_seen_at"], "2026-11-21")
                self.assertEqual(results[0]["saved_at"], results[0]["first_seen_at"])

    def test_tweet_display_in_english_preserves_stored_text(self):
        self.insert(
            "1", list="bookmark", text="你好世界",
            title="中文标题",
            meta={"quoted": {"text": "Hola mundo"}, "thread": [{"text": "こんにちは世界"}]},
        )
        self.insert("2", list="bookmark")
        with patch.object(translate, "_translate_text", return_value="English translation"):
            item = api.get_item(self.conn, "x:1")
            self.assertEqual(item["text"], "English translation")
            self.assertEqual(item["title"], "English translation")
            self.assertEqual(item["meta"]["quoted"]["text"], "English translation")
            self.assertEqual(item["meta"]["thread"][0]["text"], "English translation")
            recent = {r["id"]: r for r in api.recent(self.conn, in_list="bookmark")}
            self.assertEqual(recent["x:1"]["text"], "English translation")
            self.assertEqual(recent["x:2"]["text"], "An English bookmark.")
            results = search.search(self.conn, "你好世界", mode="keyword")["results"]
            self.assertEqual(results[0]["id"], "x:1")
            self.assertIn("English translation", results[0]["snippet"])
            self.assertNotIn("你好世界", results[0]["snippet"])
        stored = db.find_item(self.conn, "x:1")
        self.assertEqual(stored["text"], "你好世界")
        self.assertEqual(json.loads(stored["meta"])["quoted"]["text"], "Hola mundo")

    def test_translation_preserves_urls_and_hides_text_when_unavailable(self):
        with patch.object(translate, "_translate_text", side_effect=OSError):
            self.assertEqual(
                translate.english("你好世界\nhttps://example.com"),
                "[English translation unavailable]\nhttps://example.com",
            )

    def test_x_language_hint_disambiguates_short_tweets(self):
        self.insert("1", text="Hola", raw={"legacy": {"lang": "es"}})
        self.insert("2", text="Hello!")
        with patch.object(translate, "_translate_text", return_value="Hello") as model:
            self.assertEqual(api.get_item(self.conn, "x:1")["text"], "Hello")
            self.assertEqual(api.get_item(self.conn, "x:2")["text"], "Hello!")
            self.assertEqual(model.call_args.args[:2], ("Hola", "es"))
