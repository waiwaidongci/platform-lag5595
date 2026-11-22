from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.source import Dataset
from src.schemas.requests import TimeSyncRequest
from src.services.audit import record
from src.services.syncs import ConcurrencyError, list_syncs, serialize, upsert_sync

router = APIRouter(prefix='/datasets/{dataset_id}/clock-syncs')


def require_dataset(db, dataset_id):
    item = db.get(Dataset, dataset_id)
    if not item:
        raise HTTPException(404, '找不到该数据批次')
    return item


@router.get('')
def index(dataset_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    require_dataset(db, dataset_id)
    return [serialize(item) for item in list_syncs(db, dataset_id)]


@router.put('')
def upsert(dataset_id: int, body: TimeSyncRequest, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    require_dataset(db, dataset_id)
    payload = body.model_dump()
    expected_version = payload.pop('version')
    try:
        item, created = upsert_sync(db, dataset_id, payload, expected_version, actor.username)
    except ConcurrencyError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    record(db, actor.username,
           f'clock_sync.{"created" if created else "updated"}', item.id,
           dataset_id=dataset_id, endpoint=item.endpoint, version=item.version)
    db.commit()
    return {**serialize(item), 'created': created}
