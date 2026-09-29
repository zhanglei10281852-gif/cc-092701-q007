from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.grading.schemas import BatchCreate, CandidateSubmit, PublishRequest, ReviewRequest, RevokeRequest
from app.grading.service import GradingService

router = APIRouter(prefix="/api/grading", tags=["技能考试成绩治理"])


def service() -> GradingService:
    return GradingService()


def _require_participant(principal: Principal) -> None:
    if not (principal.can("grades.compute") or principal.can("grades.review") or principal.can("grades.publish")):
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError("当前账号不属于成绩治理参与方")


@router.post("/batches", status_code=201)
def create_batch(payload: BatchCreate, principal: Principal = Depends(current_principal)):
    _require_participant(principal)
    return service().create_batch(payload.model_dump(), principal.display_name)


@router.get("/batches/{code}")
def get_batch(code: str, principal: Principal = Depends(current_principal)):
    _require_participant(principal)
    return service().get_batch_view(code)


@router.post("/batches/{code}/candidates", status_code=201)
def submit_candidate(code: str, payload: CandidateSubmit, principal: Principal = Depends(current_principal)):
    return service().submit_candidate(code, payload.model_dump(), principal)


@router.get("/batches/{code}/comparison")
def compare(
    code: str,
    candidate_id: int | None = Query(default=None, ge=1),
    baseline_id: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(current_principal),
):
    _require_participant(principal)
    return service().compare(code, candidate_id, baseline_id)


@router.post("/batches/{code}/candidates/{candidate_id}/review")
def review_candidate(code: str, candidate_id: int, payload: ReviewRequest, principal: Principal = Depends(current_principal)):
    return service().review_candidate(code, candidate_id, payload.model_dump(), principal)


@router.post("/batches/{code}/candidates/{candidate_id}/publish")
def publish_candidate(code: str, candidate_id: int, payload: PublishRequest, principal: Principal = Depends(current_principal)):
    return service().publish_candidate(code, candidate_id, payload.model_dump(), principal)


@router.post("/batches/{code}/revoke")
def revoke_published(code: str, payload: RevokeRequest, principal: Principal = Depends(current_principal)):
    return service().revoke_published(code, payload.model_dump(), principal)
