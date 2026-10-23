import unittest

import acceptance_probe


class AcceptanceProbeTests(unittest.TestCase):
    def test_pipeline_flag(self):
        self.assertTrue(acceptance_probe.ACCEPTANCE_PIPELINE_OK)


if __name__ == "__main__":
    unittest.main()
