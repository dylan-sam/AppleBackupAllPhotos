"""Check that a backup password is not exposed by diagnostic representations."""

import unittest
from pathlib import Path

from iphone_copy.gui import CopySettings


class PrivacyTests(unittest.TestCase):
    """Guard against accidental password exposure in debug output."""

    def test_password_is_hidden_from_settings_repr(self):
        """Keep the supplied value out of a settings representation."""
        sentinel = "private-test-value"
        settings = CopySettings("backup", Path("output"), Path("backup"), sentinel)
        self.assertNotIn(sentinel, repr(settings))


if __name__ == "__main__":
    unittest.main()
