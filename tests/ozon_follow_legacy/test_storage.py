from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from erp.modules.ozon_follow.legacy.storage import ProductRepository
from erp.modules.ozon_follow.legacy.models import ProductCandidate


class ProductRepositoryTests(unittest.TestCase):
    def test_sourcing_queue_survives_repository_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "products.db"
            repository = ProductRepository(path)
            repository.enqueue_sourcing_job("SKU-1", 12)

            reopened = ProductRepository(path)
            self.assertEqual(reopened.list_sourcing_queue()[0]["source_sku"], "SKU-1")
            self.assertEqual(reopened.list_sourcing_queue()[0]["candidate_count"], 12)

            reopened.remove_sourcing_job("SKU-1")
            self.assertEqual(repository.list_sourcing_queue(), [])

    def test_ozon_category_and_dictionary_values_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProductRepository(Path(directory) / "products.db")
            definitions = [{"id": 10, "name": "类型"}]
            values = [{"id": 20, "value": "Кружка"}]
            self.assertIsNone(repository.get_category_attributes_cache(1, 2, "RU"))
            repository.save_category_attributes_cache(1, 2, "RU", definitions)
            repository.save_attribute_values_cache(1, 2, 10, "RU", values)
            self.assertEqual(repository.get_category_attributes_cache(1, 2, "RU"), definitions)
            self.assertEqual(repository.get_attribute_values_cache(1, 2, 10, "RU"), values)

    def test_expired_ozon_attribute_cache_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "products.db"
            repository = ProductRepository(path)
            repository.save_category_attributes_cache(1, 2, "RU", [{"id": 10}])
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE ozon_category_attribute_cache SET updated_at = ?",
                    ("2020-01-01T00:00:00",),
                )
                connection.commit()
            self.assertIsNone(repository.get_category_attributes_cache(1, 2, "RU"))

    def test_listing_drafts_can_be_enumerated_for_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProductRepository(Path(directory) / "products.db")
            repository.upsert_many([
                ProductCandidate(sku="SKU-1", source_file="a.xlsx", source_row=2, title="一", raw_data={}),
            ])
            repository.save_listing_draft("SKU-1", {
                "source_sku": "SKU-1", "offer_id": "AUTO-1", "source_ozon_url": "",
                "title": "商品一", "exported_category": "类目", "status": "IMPORT_PENDING",
                "task_id": 123,
            })
            drafts = repository.list_listing_drafts()
            self.assertEqual(len(drafts), 1)
            self.assertEqual((drafts[0]["source_sku"], drafts[0]["task_id"]), ("SKU-1", 123))

    def test_delete_product_removes_only_its_local_workflow_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProductRepository(Path(directory) / "products.db")
            first = ProductCandidate(
                sku="SKU-1", source_file="test.xlsx", source_row=2,
                title="商品一", raw_data={},
            )
            second = ProductCandidate(
                sku="SKU-2", source_file="test.xlsx", source_row=3,
                title="商品二", raw_data={},
            )
            repository.upsert_many([first, second])
            repository.save_listing_draft("SKU-1", {
                "source_sku": "SKU-1", "offer_id": "AUTO-1", "source_ozon_url": "",
                "title": "商品一", "exported_category": "类目", "status": "DRAFT",
            })
            repository.enqueue_sourcing_job("SKU-1", 10)

            self.assertTrue(repository.delete_product("SKU-1"))
            self.assertIsNone(repository.get_product_by_sku("SKU-1"))
            self.assertIsNone(repository.get_listing_draft("SKU-1"))
            self.assertEqual(repository.list_sourcing_queue(), [])
            self.assertIsNotNone(repository.get_product_by_sku("SKU-2"))
            self.assertFalse(repository.delete_product("SKU-1"))

    def test_offer_ids_are_unique_daily_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProductRepository(Path(directory) / "products.db")
            today = datetime(2026, 8, 30, 12, 0, 0)

            with ThreadPoolExecutor(max_workers=6) as executor:
                offer_ids = list(executor.map(
                    lambda _index: repository.allocate_offer_id(at=today),
                    range(12),
                ))

            self.assertEqual(len(set(offer_ids)), 12)
            self.assertEqual(
                sorted(int(value.rsplit("-", 1)[1]) for value in offer_ids),
                list(range(1, 13)),
            )
            self.assertEqual(repository.allocate_offer_id(at=today), "AUTO-20260830-013")
            self.assertEqual(
                repository.allocate_offer_id(at=datetime(2026, 8, 31)),
                "AUTO-20260831-001",
            )

    def test_batch_delete_removes_selected_products_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = ProductRepository(Path(directory) / "products.db")
            repository.upsert_many([
                ProductCandidate(sku="SKU-1", source_file="a.xlsx", source_row=2, title="一", raw_data={}),
                ProductCandidate(sku="SKU-2", source_file="a.xlsx", source_row=3, title="二", raw_data={}),
                ProductCandidate(sku="SKU-3", source_file="a.xlsx", source_row=4, title="三", raw_data={}),
            ])
            repository.enqueue_sourcing_job("SKU-1", 10)
            repository.enqueue_sourcing_job("SKU-2", 10)
            repository.enqueue_sourcing_job("SKU-3", 10)
            deleted = repository.delete_products(["SKU-1", "SKU-3", "SKU-1"])
            self.assertEqual(set(deleted), {"SKU-1", "SKU-3"})
            self.assertIsNone(repository.get_product_by_sku("SKU-1"))
            self.assertIsNotNone(repository.get_product_by_sku("SKU-2"))
            self.assertIsNone(repository.get_product_by_sku("SKU-3"))
            self.assertEqual(
                [item["source_sku"] for item in repository.list_sourcing_queue()],
                ["SKU-2"],
            )


if __name__ == "__main__":
    unittest.main()
