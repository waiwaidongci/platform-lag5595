"""对时接口与排队快照的 HTTP 集成测试（FastAPI TestClient + SQLite）。

运行：
  PYTHONPATH=src:/tmp/pylibs DATABASE_URL=sqlite:// JWT_SECRET=test \
  ARTIFACT_DIR=/tmp/artifacts python -m unittest tests.test_http_flows -v
"""

import os
import tempfile
import unittest
from datetime import datetime
from io import StringIO
import csv

_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file.name}"
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ARTIFACT_DIR", "/tmp/artifacts")

from fastapi.testclient import TestClient

from src.config.database import Base, Session, engine
from src.domain.demo import demo_rows
from src.main import app
from src.models.identity import User
from src.utils.security import hash_password


def login(client, username):
    response = client.post("/auth/login", json={"username": username, "password": f"{username}-demo-2611"})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class HttpFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        with Session.begin() as db:
            for role in ("admin", "analyst", "viewer"):
                if not db.get(User, role):
                    db.add(User(username=role, role=role, password_hash=hash_password(f"{role}-demo-2611")))
        cls.client = TestClient(app)
        cls.admin = login(cls.client, "admin")
        cls.analyst = login(cls.client, "analyst")
        cls.viewer = login(cls.client, "viewer")

    def _import_demo(self):
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=[
            "platform", "fan_id", "timestamp", "sound_time", "sound_dba", "fan_hz"])
        writer.writeheader()
        rows = demo_rows()
        for row in rows:
            writer.writerow({**row, "sound_time": row["timestamp"]})
        response = self.client.post("/datasets", headers=auth(self.analyst), json={
            "name": f"集成批次 {datetime.now().isoformat()}", "filename": "demo.csv",
            "csv_text": buffer.getvalue()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_full_clock_sync_to_results_flow(self):
        dataset_id = self._import_demo()
        # 只读角色不能写对时。
        denied = self.client.put(f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.viewer),
                                 json={"endpoint": "start", "observed_at": "2026-11-21 09:25:00",
                                       "deviation_low": -7, "deviation_high": -3})
        self.assertEqual(denied.status_code, 403)

        start_payload = {"endpoint": "start", "observed_at": "2026-11-21 09:25:00",
                         "deviation_low": -7, "deviation_high": -3, "note": "开班"}
        end_payload = {"endpoint": "end", "observed_at": "2026-11-21 09:50:00",
                       "deviation_low": -7, "deviation_high": -3, "note": "收班"}
        created = self.client.put(f"/datasets/{dataset_id}/clock-syncs",
                                  headers=auth(self.analyst), json=start_payload)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertTrue(created.json()["created"])
        created_end = self.client.put(f"/datasets/{dataset_id}/clock-syncs",
                                      headers=auth(self.admin), json=end_payload)
        self.assertTrue(created_end.json()["created"])

        listed = self.client.get(f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.viewer))
        self.assertEqual([item["endpoint"] for item in listed.json()], ["start", "end"])

        # 并发更正：两端各基于 v1 提交，均成功且互不覆盖。
        corrected_start = self.client.put(
            f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.analyst),
            json={**start_payload, "deviation_low": -8, "note": "首端更正", "version": 1})
        self.assertEqual(corrected_start.status_code, 200, corrected_start.text)
        corrected_end = self.client.put(
            f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.admin),
            json={**end_payload, "deviation_high": -2, "note": "末端更正", "version": 1})
        self.assertEqual(corrected_end.status_code, 200, corrected_end.text)

        # 同端再拿旧版本号提交应被 409 拦截。
        stale = self.client.put(
            f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.analyst),
            json={**start_payload, "note": "过期更正", "version": 1})
        self.assertEqual(stale.status_code, 409)

        current = {item["endpoint"]: item for item in
                   self.client.get(f"/datasets/{dataset_id}/clock-syncs",
                                   headers=auth(self.viewer)).json()}
        self.assertEqual(current["start"]["note"], "首端更正")
        self.assertEqual(current["start"]["version"], 2)
        self.assertEqual(current["end"]["note"], "末端更正")
        self.assertEqual(current["end"]["version"], 2)

        # 非法区间（下限大于上限）返回 422。
        bad = self.client.put(
            f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.analyst),
            json={**start_payload, "deviation_low": 9, "deviation_high": 1, "version": 2})
        self.assertEqual(bad.status_code, 422)

        # 排队：快照写入运行参数。
        queued = self.client.post("/runs", headers=auth(self.analyst), json={
            "dataset_id": dataset_id, "name": "对时集成运行",
            "params": {"sample_seconds": 10, "max_lag": 12, "min_pairs": 30}})
        self.assertEqual(queued.status_code, 202, queued.text)
        run_id = queued.json()["id"]
        snapshot = queued.json()["params"]["clock_sync"]
        self.assertEqual([item["endpoint"] for item in snapshot], ["start", "end"])

        # 排队后再更正对时，运行参数里的快照不变（扫描重试不更换）。
        self.client.put(f"/datasets/{dataset_id}/clock-syncs", headers=auth(self.analyst),
                        json={**start_payload, "note": "排队后的新更正", "version": 2})
        run_view = self.client.get(f"/runs/{run_id}", headers=auth(self.viewer)).json()
        self.assertEqual(run_view["params"]["clock_sync"][0]["note"], "首端更正")

        # 直接驱动 worker 执行链，验证结果结构与复测判定。
        from src.services.execution import claim, execute
        lease = claim()
        self.assertIsNotNone(lease)
        execute(lease)
        finished = self.client.get(f"/runs/{run_id}", headers=auth(self.viewer)).json()
        self.assertEqual(finished["status"], "complete", finished.get("error"))
        result = finished["result"]
        self.assertIn("retest_recommended", result)
        titles = [table["title"] for table in result["tables"]]
        self.assertIn("漂移场景峰档汇总", titles)
        self.assertIn("可能峰档（按支持场景数排序）", titles)
        self.assertIn("无峰原因", titles)
        self.assertGreaterEqual(result["metrics"][0]["value"], 1)


if __name__ == "__main__":
    unittest.main()
