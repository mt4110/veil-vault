"""Regressions for conditions that must prevent a production deployment."""

from copy import deepcopy
from pathlib import Path
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_deploy import ROOT, validate


class DeploymentGateTests(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads((ROOT / "wrangler.production.toml").read_text())
        self.config["d1_databases"][0]["database_id"] = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"

    def test_valid_closed_and_enabled_config(self):
        self.assertEqual(validate(self.config), [])
        self.config["vars"]["SERVICE_ENABLED"] = "true"
        self.assertEqual(validate(self.config), [])

    def test_unsafe_config_is_rejected(self):
        cases = (
            ("workers_dev", True), ("preview_urls", True),
            ("routes", [{"pattern": "other.example.com", "custom_domain": True}]),
            ("d1_databases", []),
            ("vars", {"SERVICE_ENABLED": "true", "PUBLISH_TOKEN": "public-fixture"}),
            ("vars", {}), ("triggers", {}), ("observability", {"enabled": True}),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                config = deepcopy(self.config)
                config[key] = value
                self.assertTrue(validate(config))

    def test_placeholder_and_malformed_database_ids(self):
        for identifier in ("00000000-0000-0000-0000-000000000000", "not-a-uuid", None):
            self.config["d1_databases"][0]["database_id"] = identifier
            self.assertTrue(validate(self.config))


if __name__ == "__main__":
    unittest.main()
