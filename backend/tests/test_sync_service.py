"""对时记录并发更正、乐观锁与排队快照的服务层测试（SQLite 内存库）。

运行：
  PYTHONPATH=src:/tmp/pylibs DATABASE_URL=sqlite:// JWT_SECRET=test \
  python -m unittest tests.test_sync_service -v
"""

import os
import unittest
from datetime import datetime

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET", "test-secret")

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.database import Base, Session, engine
from src.models.source import Dataset
from src.models.sync import TimeSync
from src.services.syncs import ConcurrencyError, list_syncs, snapshot, upsert_sync

START = {"endpoint": "start", "observed_at": "2026-11-21 09:25:00",
         "deviation_low": -7.0, "deviation_high": -3.0, "note": "开班"}
END = {"endpoint": "end", "observed_at": "2026-11-21 09:50:00",
       "deviation_low": -5.0, "deviation_high": -1.0, "note": "收班"}


class SyncServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)

    def setUp(self):
        with Session.begin() as db:
            db.query(TimeSync).delete()
            db.query(Dataset).delete()
            db.add(Dataset(name="批次甲", filename="a.csv", checksum="hash-a", rows=[], row_count=0, author="seed"))
            db.add(Dataset(name="批次乙", filename="b.csv", checksum="hash-b", rows=[], row_count=0, author="seed"))

    def _dataset(self, name="批次甲"):
        with Session() as db:
            return db.scalar(select(Dataset).where(Dataset.name == name)).id

    def test_create_and_update_bumps_version(self):
        dataset_id = self._dataset()
        with Session.begin() as db:
            item, created = upsert_sync(db, dataset_id, START, None, "analyst")
            self.assertTrue(created)
            self.assertEqual(item.version, 1)
        with Session.begin() as db:
            revised = {**START, "deviation_low": -8.0, "deviation_high": -4.0}
            item, created = upsert_sync(db, dataset_id, revised, 1, "admin")
            self.assertFalse(created)
            self.assertEqual(item.version, 2)
            self.assertEqual(item.deviation_low, -8.0)
        with Session() as db:
            self.assertEqual(len(list_syncs(db, dataset_id)), 1)

    def test_stale_version_is_rejected(self):
        dataset_id = self._dataset()
        with Session.begin() as db:
            upsert_sync(db, dataset_id, START, None, "analyst")
        with Session.begin() as db:
            upsert_sync(db, dataset_id, {**START, "note": "第一次更正"}, 1, "analyst")
        with self.assertRaises(ConcurrencyError):
            with Session.begin() as db:
                upsert_sync(db, dataset_id, {**START, "note": "基于旧版本的更正"}, 1, "admin")

    def test_update_without_version_is_rejected(self):
        # 记录已存在时，不带版本号的覆盖请求必须被拒绝，避免抹掉他人更正。
        dataset_id = self._dataset()
        with Session.begin() as db:
            upsert_sync(db, dataset_id, START, None, "analyst")
        with self.assertRaises(ConcurrencyError):
            with Session.begin() as db:
                upsert_sync(db, dataset_id, {**START, "note": "无版本号覆盖"}, None, "admin")

    def test_create_with_explicit_zero_version_succeeds(self):
        dataset_id = self._dataset()
        with Session.begin() as db:
            item, created = upsert_sync(db, dataset_id, START, 0, "analyst")
        self.assertTrue(created)
        self.assertEqual(item.version, 1)

    def test_concurrent_corrections_on_different_endpoints_both_kept(self):
        # 核心口径：分属两端的并发更正互不覆盖。
        dataset_id = self._dataset()
        with Session.begin() as db:
            upsert_sync(db, dataset_id, START, None, "analyst")
            upsert_sync(db, dataset_id, END, None, "admin")
        # 操作者 A 更正首端、操作者 B 同时更正末端，各自基于 version=1。
        with Session.begin() as db:
            start_item, _ = upsert_sync(db, dataset_id, {**START, "note": "A 的首端更正"}, 1, "analyst-a")
        with Session.begin() as db:
            end_item, _ = upsert_sync(db, dataset_id, {**END, "note": "B 的末端更正"}, 1, "analyst-b")
        with Session() as db:
            rows = {item.endpoint: item for item in list_syncs(db, dataset_id)}
            self.assertEqual(rows["start"].note, "A 的首端更正")
            self.assertEqual(rows["start"].version, 2)
            self.assertEqual(rows["end"].note, "B 的末端更正")
            self.assertEqual(rows["end"].version, 2)

    def test_other_dataset_rows_are_isolated(self):
        first = self._dataset("批次甲")
        second = self._dataset("批次乙")
        with Session.begin() as db:
            upsert_sync(db, first, START, None, "analyst")
        with Session.begin() as db:
            item, created = upsert_sync(db, second, START, None, "analyst")
            self.assertTrue(created)

    def test_pair_order_validation_on_upsert(self):
        dataset_id = self._dataset()
        with Session.begin() as db:
            upsert_sync(db, dataset_id, START, None, "analyst")
        with self.assertRaises(ValueError):
            with Session.begin() as db:
                upsert_sync(db, dataset_id,
                            {**END, "observed_at": "2026-11-21 09:00:00"}, None, "analyst")

    def test_snapshot_returns_none_until_both_endpoints_exist(self):
        dataset_id = self._dataset()
        with Session() as db:
            self.assertIsNone(snapshot(db, dataset_id))
        with Session.begin() as db:
            upsert_sync(db, dataset_id, START, None, "analyst")
        with Session() as db:
            self.assertIsNone(snapshot(db, dataset_id))
        with Session.begin() as db:
            upsert_sync(db, dataset_id, END, None, "admin")
        with Session() as db:
            snap = snapshot(db, dataset_id)
        self.assertEqual([item["endpoint"] for item in snap], ["start", "end"])
        self.assertEqual(snap[0]["deviation_high"], -3.0)

    def test_unique_constraint_per_dataset_and_endpoint(self):
        dataset_id = self._dataset()
        with Session.begin() as db:
            upsert_sync(db, dataset_id, START, None, "analyst")
        with self.assertRaises(IntegrityError):
            with Session.begin() as db:
                db.add(TimeSync(dataset_id=dataset_id, endpoint="start",
                                observed_at=datetime(2026, 11, 21, 9, 25),
                                deviation_low=0, deviation_high=0, note="", author="x"))


if __name__ == "__main__":
    unittest.main()
