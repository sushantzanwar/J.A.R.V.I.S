"""Retrieval-quality evaluation and latency benchmarks for J.A.R.V.I.S. FTS5 & ContextManager.

Evaluates:
  1. Exact fact matching
  2. Paraphrased natural language question recall
  3. Stale vs updated fact disambiguation
  4. Category priority (identity/preference outranking general notes/noise)
  5. Noise resistance against unrelated memories
  6. Empty / stopword-only queries behavior
  7. Large-store scale test (1,000 memories + 1,000 messages) with latency check
"""
import time
import unittest

from jarvis.core.context import ContextManager
from jarvis.core.store import Store


class RetrievalBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)  # In-memory store
        self.addCleanup(self.store.close)

    def test_1_exact_fact_match(self):
        """Exact keyword query directly retrieves the corresponding memory."""
        self.store.remember("editor", "vscode text editor", category="preference")
        self.store.remember("theme", "dracula dark mode", category="preference")

        results = self.store.search_memories("editor")
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["key"], "editor")
        self.assertIn("vscode", results[0]["value"])

    def test_2_paraphrased_natural_language_query(self):
        """Conversational phrasing with filler words ('what code editor do I use?') correctly retrieves the memory."""
        self.store.remember("editor", "vscode", category="preference")
        self.store.remember("shell", "powershell", category="preference")

        # Conversational queries with stopwords filtered
        results = self.store.search_memories("what code editor do I use?")
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["key"], "editor")
        self.assertEqual(results[0]["value"], "vscode")

    def test_3_stale_fact_disambiguation(self):
        """When a fact is updated, search only returns the current active value, not the overwritten stale value."""
        self.store.remember("editor", "vscode", category="preference")
        self.store.remember("editor", "neovim", category="preference")

        # Active search must yield the latest value
        results = self.store.search_memories("editor")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "neovim")

        # History still preserves the stale record for audit
        history = self.store.memory_history(key="editor")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["new_value"], "neovim")
        self.assertEqual(history[0]["old_value"], "vscode")

    def test_4_category_priority(self):
        """Identity and preference memories outrank general notes or random messages."""
        self.store.remember("name", "Tony Stark", category="identity")
        self.store.remember("side_note", "Tony Stark mentioned an appointment", category="general")
        self.store.add_message("user", "Tony Stark was talking about suits")

        # When ranking via ContextManager, the identity category fact should score highest
        top_memories = ContextManager.get_relevant_memories(self.store, "Tony Stark", limit=3)
        self.assertTrue(len(top_memories) >= 2)
        # First memory should be the identity fact due to category priority weighting
        self.assertEqual(top_memories[0]["category"], "identity")
        self.assertEqual(top_memories[0]["key"], "name")
        self.assertGreater(top_memories[0]["composite_score"], top_memories[1]["composite_score"])

    def test_5_noise_resistance(self):
        """Searching for a specific topic must not return unrelated memories."""
        self.store.remember("car", "Audi R8 Spyder", category="device")
        self.store.remember("workout_goal", "50 pushups every morning", category="goal")
        self.store.remember("favorite_food", "cheeseburger", category="preference")

        results = self.store.search_memories("python programming editor")
        # None of the completely unrelated memories should match
        self.assertEqual(len(results), 0)

    def test_6_empty_and_stopword_only_query_safety(self):
        """Empty, punctuation-only, or stopword-only queries must never crash or throw FTS5 syntax errors."""
        self.store.remember("project", "Mark 85 armor", category="project")

        for query in ("", "   ", "???!!!***", "what is the", "do you show me"):
            results = self.store.search_memories(query)
            self.assertIsInstance(results, list)

            messages = self.store.search_messages(query)
            self.assertIsInstance(messages, list)

    def test_7_large_store_needle_in_haystack_and_latency(self):
        """Scale benchmark: 1,000 synthetic memories and 1,000 messages.

        Measures search latency (<25ms) and verifies needle retrieval.
        """
        # Seed 1,000 background memories
        distractor_categories = ["goal", "project", "device", "contact", "general"]
        for i in range(1000):
            cat = distractor_categories[i % len(distractor_categories)]
            self.store.remember(f"item_{i}", f"Synthetic data payload number {i} for stress testing", category=cat)

        # Seed the needle
        self.store.remember("secret_passcode", "arc_reactor_core_alpha_99", category="identity")

        # Seed 1,000 messages
        for i in range(1000):
            self.store.add_message("user", f"Synthetic conversational chat filler turn {i}")

        self.store.add_message("jarvis", "Special directive: Initiate protocol veritas prime.")

        # Benchmark 1: Search needle in 1,000+ memories
        t0 = time.perf_counter()
        needle_mems = self.store.search_memories("arc reactor alpha")
        latency_mem_ms = (time.perf_counter() - t0) * 1000

        self.assertTrue(len(needle_mems) >= 1)
        self.assertEqual(needle_mems[0]["key"], "secret_passcode")
        self.assertIn("arc_reactor_core_alpha_99", needle_mems[0]["value"])
        self.assertLess(latency_mem_ms, 25.0, f"Memory search latency too high: {latency_mem_ms:.2f}ms")

        # Benchmark 2: Search needle in 1,000+ messages
        t1 = time.perf_counter()
        needle_msgs = self.store.search_messages("veritas prime")
        latency_msg_ms = (time.perf_counter() - t1) * 1000

        self.assertTrue(len(needle_msgs) >= 1)
        self.assertIn("veritas prime", needle_msgs[0]["text"])
        self.assertLess(latency_msg_ms, 25.0, f"Message search latency too high: {latency_msg_ms:.2f}ms")


if __name__ == "__main__":
    unittest.main()
