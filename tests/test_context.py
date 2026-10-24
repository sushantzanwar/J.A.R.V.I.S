"""Unit tests for ContextManager, context assembly, and offline selective summarization."""
import unittest

from jarvis.core.context import ContextManager
from jarvis.core.store import Store


class ContextManagerTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)  # In-memory store
        self.addCleanup(self.store.close)

    def test_get_recent_turns_empty_and_populated(self):
        self.assertEqual(ContextManager.get_recent_turns(self.store), [])
        self.assertEqual(ContextManager.get_recent_turns(None), [])

        self.store.add_message("user", "turn 1")
        self.store.add_message("jarvis", "response 1")
        turns = ContextManager.get_recent_turns(self.store, limit=5)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["text"], "turn 1")
        self.assertEqual(turns[1]["text"], "response 1")

    def test_get_relevant_memories_fts_and_fallback(self):
        self.store.remember("name", "Tony Stark", category="identity")
        self.store.remember("editor", "vscode", category="preference")
        self.store.remember("car", "Audi R8", category="general")

        # FTS match should retrieve matching memory first
        mems = ContextManager.get_relevant_memories(self.store, "what is my name", limit=2)
        self.assertTrue(len(mems) >= 1)
        self.assertEqual(mems[0]["key"], "name")

        # Fallback supplements when query matches fewer than limit
        mems_fallback = ContextManager.get_relevant_memories(self.store, "something unrelated", limit=2)
        self.assertEqual(len(mems_fallback), 2)

    def test_format_context_assembly(self):
        self.store.remember("theme", "dark", category="preference")
        self.store.add_message("user", "hi")
        self.store.add_message("jarvis", "hello")
        self.store.add_summary(1, 2, "Conversation began with greetings.", ["greeted"])

        ctx = ContextManager.format_context(self.store, "set theme to light", max_turns=4, max_memories=3)
        self.assertIn("query", ctx)
        self.assertIn("summary", ctx)
        self.assertIn("memories", ctx)
        self.assertIn("recent_turns", ctx)
        self.assertIn("prompt_context", ctx)

        prompt = ctx["prompt_context"]
        self.assertIn("SYSTEM NOTICE", prompt)
        self.assertIn('<context_data type="user_memories">', prompt)
        self.assertIn('<memory category="preference" key="theme">dark</memory>', prompt)
        self.assertIn('<context_data type="conversation_summary"', prompt)
        self.assertIn("Conversation began with greetings.", prompt)
        self.assertIn('<context_data type="recent_turns">', prompt)
        self.assertIn("<User>hi</User>", prompt)
        self.assertIn("<Jarvis>hello</Jarvis>", prompt)
        self.assertIn("User Query: set theme to light", prompt)

    def test_summarize_messages_extractive_rules(self):
        messages = [
            {"role": "user", "text": "remember my city is Malibu"},
            {"role": "jarvis", "text": "I'll remember that your city is Malibu."},
            {"role": "user", "text": "open notepad"},
            {"role": "jarvis", "text": "Opened notepad."},
            {"role": "user", "text": "mute volume"},
            {"role": "jarvis", "text": "Volume muted."},
            {"role": "user", "text": "lock workstation"},
            {"role": "jarvis", "text": "Workstation locked."},
            {"role": "user", "text": "show tasks in queue"},
            {"role": "jarvis", "text": "0 tasks on record."},
        ]

        extracted = ContextManager.summarize_messages(messages)
        self.assertIn("summary", extracted)
        self.assertIn("key_points", extracted)

        summary = extracted["summary"]
        self.assertIn("10 messages", summary)
        self.assertIn("5 user turns", summary)

        points = extracted["key_points"]
        self.assertTrue(any("recorded memory" in p for p in points))
        self.assertTrue(any("launched application" in p for p in points))
        self.assertTrue(any("System control" in p for p in points))
        self.assertTrue(any("Workstation locked" in p for p in points))
        self.assertTrue(any("Task queue" in p for p in points))

    def test_summarize_conversation_and_store_recording(self):
        self.store.add_message("user", "remember favorite color is blue")
        self.store.add_message("jarvis", "saved")

        summary_record = ContextManager.summarize_conversation(self.store)
        self.assertIsNotNone(summary_record)
        self.assertEqual(summary_record["id"], 1)
        self.assertIn("Conversation consisting of 2 messages", summary_record["summary"])

        stored = self.store.summaries(limit=1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["summary"], summary_record["summary"])

    def test_auto_summarize_if_needed(self):
        # With < 10 messages, no auto-summarize
        for i in range(5):
            self.store.add_message("user", f"message {i}")
            self.store.add_message("jarvis", f"reply {i}")

        result = ContextManager.auto_summarize_if_needed(self.store, threshold=20)
        self.assertIsNone(result)

        # Reach threshold
        for i in range(5, 12):
            self.store.add_message("user", f"message {i}")
            self.store.add_message("jarvis", f"reply {i}")

        result2 = ContextManager.auto_summarize_if_needed(self.store, threshold=20)
        self.assertIsNotNone(result2)
        self.assertEqual(result2["id"], 1)


if __name__ == "__main__":
    unittest.main()
