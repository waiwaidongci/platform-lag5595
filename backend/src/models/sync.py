from datetime import datetime
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class TimeSync(Base):
    """数据批次首尾对时记录，每个批次每个端别（start/end）各一行。"""

    __tablename__ = 'time_syncs'
    __table_args__ = (UniqueConstraint('dataset_id', 'endpoint'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    endpoint: Mapped[str] = mapped_column(String(10))
    observed_at: Mapped[datetime]
    deviation_low: Mapped[float]
    deviation_high: Mapped[float]
    note: Mapped[str] = mapped_column(String(300), default='')
    # 乐观锁：并发更正时各自保留分属两端的改动，同端并发以版本号拦截。
    version: Mapped[int] = mapped_column(default=1)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now)
