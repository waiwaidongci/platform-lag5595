import json
from sqlalchemy import func, select
from src.config.database import Session
from src.constants.enums import Role
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.source import Dataset
from src.models.sync import TimeSync
from src.services.datasets import save
from src.services.syncs import upsert_sync
from src.utils.security import hash_password


# 演示批次的首尾对时：声级计偏慢约半格，偏差本身仍有 ±2 秒不确定区间。
DEMO_CLOCK_SYNCS = [
    {"endpoint": "start", "observed_at": "2026-11-21 09:25:00",
     "deviation_low": -7.0, "deviation_high": -3.0, "note": "开班对时，声级计偏慢"},
    {"endpoint": "end", "observed_at": "2026-11-21 09:50:00",
     "deviation_low": -7.0, "deviation_high": -3.0, "note": "收班对时，偏差与开班接近"},
]


def seed():
    with Session.begin() as db:
        for role in Role:
            if not db.get(User, role.value):
                db.add(User(username=role.value, role=role, password_hash=hash_password(f'{role.value}-demo-2611')))
        item, created = save(db, SPEC['default_dataset'], SPEC['sample_name'], demo_rows(), 'seed')
        for payload in DEMO_CLOCK_SYNCS:
            existing = db.scalar(select(TimeSync).where(TimeSync.dataset_id == item.id,
                                                        TimeSync.endpoint == payload['endpoint']))
            # 内容未变则跳过，保证种子可安全重复执行而不推高版本号。
            unchanged = (existing is not None
                         and existing.deviation_low == payload['deviation_low']
                         and existing.deviation_high == payload['deviation_high']
                         and existing.observed_at.isoformat(sep=' ') == payload['observed_at']
                         and (existing.note or '') == payload['note'])
            if not unchanged:
                upsert_sync(db, item.id, payload, None if existing is None else existing.version, 'seed')
        return {'users': db.scalar(select(func.count()).select_from(User)),
                'datasets': db.scalar(select(func.count()).select_from(Dataset)),
                'seed_dataset_id': item.id, 'row_count': item.row_count, 'checksum': item.checksum,
                'created_dataset': created}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
