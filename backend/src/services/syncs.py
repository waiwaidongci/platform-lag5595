"""对时记录的存取与并发更正。

并发策略：start/end 是两个独立行（唯一约束 dataset_id+endpoint），
两个操作者同时更正不同端别时各自提交、互不覆盖；同一端别的并发更正
以 version 乐观锁拦截，要求先刷新再提交。
"""

from sqlalchemy import select
from src.domain.clock import parse_instant, validate_clock_sync, validate_pair
from src.models.sync import TimeSync
from src.utils.time import now


def list_syncs(db, dataset_id):
    items = list(db.scalars(
        select(TimeSync).where(TimeSync.dataset_id == dataset_id)))
    # 端别按业务顺序（首端 start、末端 end）返回，不依赖字符串排序。
    order = {endpoint: index for index, endpoint in enumerate(("start", "end"))}
    return sorted(items, key=lambda item: order.get(item.endpoint, len(order)))


def serialize(item):
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "endpoint": item.endpoint,
        "observed_at": item.observed_at.isoformat(sep=" "),
        "deviation_low": item.deviation_low,
        "deviation_high": item.deviation_high,
        "note": item.note,
        "version": item.version,
        "author": item.author,
        "updated_at": item.updated_at.isoformat(sep=" ") if item.updated_at else None,
    }


def upsert_sync(db, dataset_id, payload, expected_version, actor):
    """按端别新建或更正对时记录，返回 (记录, 是否新建)。

    expected_version 为 None 表示仅在不存在时新建；已存在时版本不符抛出 409 语义错误。
    另一条端别的记录在此过程中不被读取或改写，保证两端并发更正各自保留。
    """
    clean = validate_clock_sync(payload)
    item = db.scalar(
        select(TimeSync).where(TimeSync.dataset_id == dataset_id,
                               TimeSync.endpoint == clean["endpoint"]).with_for_update())
    created = item is None
    if created:
        if expected_version not in (None, 0):
            raise ConcurrencyError("该对时记录版本已变化，请刷新后重试。")
        current_version = 0
    else:
        if expected_version is None:
            raise ConcurrencyError(
                f"{'首次' if clean['endpoint'] == 'start' else '末次'}对时记录已存在，"
                "请基于最新版本提交更正。")
        current_version = item.version
        if expected_version != current_version:
            raise ConcurrencyError(
                f"{'首次' if clean['endpoint'] == 'start' else '末次'}对时记录已被他人更正，"
                "请刷新查看最新版本后再提交；另一端别的改动会原样保留。")
    if created:
        item = TimeSync(dataset_id=dataset_id, endpoint=clean["endpoint"], author=actor, version=1)
        db.add(item)
    item.observed_at = parse_instant(clean["observed_at"], "对时观测时刻")
    item.deviation_low = clean["deviation_low"]
    item.deviation_high = clean["deviation_high"]
    item.note = clean["note"]
    item.author = actor
    item.version = current_version + 1
    item.updated_at = now()
    db.flush()
    # 联合校验仅在两端记录都存在时进行，不修改另一端的任何字段。
    other = db.scalar(
        select(TimeSync).where(TimeSync.dataset_id == dataset_id,
                               TimeSync.endpoint != clean["endpoint"]))
    if other is not None:
        pair = {clean["endpoint"]: clean,
                other.endpoint: {
                    "endpoint": other.endpoint,
                    "observed_at": other.observed_at.isoformat(sep=" "),
                    "deviation_low": other.deviation_low,
                    "deviation_high": other.deviation_high,
                    "note": other.note,
                }}
        validate_pair(pair["start"], pair["end"])
    return item, created


def snapshot(db, dataset_id):
    """运行排队时快照首尾对时；记录不齐返回 None（按零偏差运行）。"""
    items = list_syncs(db, dataset_id)
    by_endpoint = {item.endpoint: item for item in items}
    if set(by_endpoint) != {"start", "end"}:
        return None
    return [
        {"endpoint": "start",
         "observed_at": by_endpoint["start"].observed_at.isoformat(sep=" "),
         "deviation_low": by_endpoint["start"].deviation_low,
         "deviation_high": by_endpoint["start"].deviation_high,
         "note": by_endpoint["start"].note},
        {"endpoint": "end",
         "observed_at": by_endpoint["end"].observed_at.isoformat(sep=" "),
         "deviation_low": by_endpoint["end"].deviation_low,
         "deviation_high": by_endpoint["end"].deviation_high,
         "note": by_endpoint["end"].note},
    ]


class ConcurrencyError(Exception):
    """对时记录版本冲突。"""
