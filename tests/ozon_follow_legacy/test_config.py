from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from erp.modules.ozon_follow.legacy.config import AppConfig, ConfigStore, _protect_secret, _unprotect_secret


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class ConfigEncryptionTests(unittest.TestCase):
    def test_new_secrets_use_machine_scope_and_round_trip(self) -> None:
        encrypted = _protect_secret("test-secret")

        self.assertTrue(encrypted.startswith("dpapi-machine:"))
        self.assertNotIn("test-secret", encrypted)
        self.assertEqual(_unprotect_secret(encrypted), "test-secret")

    def test_plain_non_secret_value_is_unchanged(self) -> None:
        self.assertEqual(_unprotect_secret("plain-value"), "plain-value")

    def test_config_save_is_atomic_and_keeps_previous_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            store.save(AppConfig(alibaba_app_key="first", ozon_api_key="secret-1"))
            store.save(AppConfig(alibaba_app_key="second", ozon_api_key="secret-2"))

            self.assertEqual(store.load().alibaba_app_key, "second")
            self.assertTrue(store.backup_path.exists())
            backup = ConfigStore(store.backup_path).load()
            self.assertEqual(backup.alibaba_app_key, "first")

    def test_damaged_primary_uses_valid_backup_without_clearing_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            store.save(AppConfig(alibaba_app_key="first", ozon_api_key="secret-1"))
            store.save(AppConfig(alibaba_app_key="second", ozon_api_key="secret-2"))
            path.write_text("{broken", encoding="utf-8")

            recovered = store.load()
            self.assertEqual(recovered.alibaba_app_key, "first")
            self.assertEqual(recovered.ozon_api_key, "secret-1")

            store.save(AppConfig(alibaba_app_key="repaired", ozon_api_key="secret-3"))
            self.assertEqual(store.load().alibaba_app_key, "repaired")
            self.assertEqual(ConfigStore(store.backup_path).load().alibaba_app_key, "first")


if __name__ == "__main__":
    unittest.main()
