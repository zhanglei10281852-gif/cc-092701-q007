from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.compute.releases import ResultReleaseService
from app.compute.schemas import (
    BatchOperation, CancelRequest, CandidateSubmit, PriorityRequest, QuotaSet,
    RetryRequest, ReviewNote, RevokeRequest, OptionalReviewNote, TaskClaim,
    TaskFailure, TaskRescore, TaskResult, TaskSubmit, TemplateCreate,
)
from app.compute.service import ComputeOperationsService
from app.core.security import Principal

router = APIRouter(prefix="/api/compute", tags=["科学计算任务运营"])


def service() -> ComputeOperationsService:
    return ComputeOperationsService()


def release_service() -> ResultReleaseService:
    return ResultReleaseService()


@router.get("/templates")
def list_templates():
    return {"items": service().list_templates()}


@router.post("/templates", status_code=201)
def create_template(payload: TemplateCreate, actor: str = Query(..., min_length=1)):
    return service().create_template(payload.model_dump(), actor)


@router.put("/quotas")
def set_quota(payload: QuotaSet, actor: str = Query(..., min_length=1)):
    return service().set_quota(payload.model_dump(), actor)


@router.post("/tasks", status_code=202)
def submit_task(payload: TaskSubmit):
    return service().submit(payload.model_dump())


@router.get("/tasks")
def list_tasks(status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=limit)}


@router.get("/task-details/{task_id}")
def get_task(task_id: int):
    return service().get_task(task_id)


@router.post("/tasks/claim")
def claim_task(payload: TaskClaim):
    return {"task": service().claim(payload.worker_id, payload.capabilities, payload.lease_seconds)}


@router.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: int, payload: TaskClaim):
    return service().heartbeat(task_id, payload.worker_id, payload.lease_seconds)


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: int, payload: TaskResult):
    return service().complete(task_id, payload.worker_id, payload.result, payload.metrics)


@router.post("/tasks/{task_id}/fail")
def fail_task(task_id: int, payload: TaskFailure):
    return service().fail(task_id, payload.worker_id, payload.error_code, payload.message, payload.retryable)


@router.post("/tasks/{task_id}/rescore")
def rescore_task(task_id: int, payload: TaskRescore):
    return service().rescore(task_id, payload.worker_id, payload.scorer_code, payload.result, payload.metrics)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, payload: CancelRequest):
    return service().cancel(task_id, payload.actor, payload.reason)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, payload: RetryRequest):
    return service().retry(task_id, payload.actor, payload.reason, payload.priority)


@router.post("/tasks/{task_id}/priority")
def set_priority(task_id: int, payload: PriorityRequest):
    return service().set_priority(task_id, payload.actor, payload.reason, payload.priority)


@router.post("/tasks/batch")
def batch_operation(payload: BatchOperation):
    return service().batch_operation(payload.model_dump())


@router.post("/recovery/expired-leases")
def recover_expired(actor: str = Query(default="recovery-worker", min_length=1)):
    return service().recover_expired(actor)


@router.get("/summary")
def summary():
    return service().summary()


# -- 成绩结果候选、复核、发布与撤销 -----------------------------------------


@router.post("/tasks/{task_id}/releases", status_code=201)
def submit_candidate(task_id: int, payload: CandidateSubmit, principal: Principal = Depends(current_principal)):
    return release_service().submit_candidate(task_id, payload.model_dump(), principal)


@router.get("/tasks/{task_id}/releases")
def release_overview(task_id: int, principal: Principal = Depends(current_principal)):
    return release_service().overview(task_id)


@router.get("/tasks/{task_id}/releases/compare")
def compare_releases(task_id: int, base: int = Query(..., ge=1), target: int = Query(..., ge=1),
                     principal: Principal = Depends(current_principal)):
    return release_service().compare(task_id, base, target)


@router.post("/releases/{release_id}/review-start")
def start_release_review(release_id: int, payload: OptionalReviewNote, principal: Principal = Depends(current_principal)):
    return release_service().start_review(release_id, principal, payload.note)


@router.post("/releases/{release_id}/approve")
def approve_release(release_id: int, payload: ReviewNote, principal: Principal = Depends(current_principal)):
    return release_service().approve(release_id, principal, payload.note)


@router.post("/releases/{release_id}/reject")
def reject_release(release_id: int, payload: ReviewNote, principal: Principal = Depends(current_principal)):
    return release_service().reject(release_id, principal, payload.note)


@router.post("/releases/{release_id}/publish")
def publish_release(release_id: int, payload: ReviewNote, principal: Principal = Depends(current_principal)):
    return release_service().publish(release_id, principal, payload.note)


@router.post("/releases/{release_id}/revoke")
def revoke_release(release_id: int, payload: RevokeRequest, principal: Principal = Depends(current_principal)):
    return release_service().revoke(release_id, principal, payload.reason)
