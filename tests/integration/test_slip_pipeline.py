import unittest


class SlipPipelineIntegrationTests(unittest.TestCase):
    @unittest.skip("Requires a future mocked Hermes gateway/Drive/Sheets harness")
    def test_confirmed_slip_is_written_exactly_once(self):
        """Reserved contract: confirmation must produce exactly one transaction."""
