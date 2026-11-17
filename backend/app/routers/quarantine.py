from fastapi import APIRouter, Query

from app.services import settle as svc

router = APIRouter(tags=["未平隔离"])


@router.get("/quarantine")
def get_quarantine(bundle_key: str | None = Query(None, description="按捆扎键过滤")):
    return svc.list_quarantine(bundle_key)
