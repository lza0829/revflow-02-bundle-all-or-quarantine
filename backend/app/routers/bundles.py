from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import settle as svc

router = APIRouter(tags=["冲正捆扎"])


class BundleDeclare(BaseModel):
    bundle_key: str = Field(..., description="捆扎键；同捆冲正共享，落库可查")
    entry_keys: list[str] = Field(..., min_length=1, description="成员冲正流水键")


@router.get("/reversal-bundles")
def get_bundles():
    return svc.list_bundles()


@router.get("/reversal-bundles/{bundle_key}")
def get_bundle(bundle_key: str):
    return svc.get_bundle(bundle_key)


@router.post("/reversal-bundles")
def post_bundle(body: BundleDeclare):
    return svc.declare_bundle(body.bundle_key, body.entry_keys)


@router.post("/reversal-bundles/{bundle_key}/settle")
def settle_bundle(bundle_key: str):
    return svc.run_bundle_settle(bundle_key)
