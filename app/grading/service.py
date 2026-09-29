from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.grading.repository import TRANSITIONS, GradingRepository


def scores_digest(scores: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    payload = json.dumps({"scores": scores, "metrics": metrics}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class GradingService:
    """成绩候选、复核、发布、撤销的应用服务，所有状态变更均在单事务内完成。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 批次

    def create_batch(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = GradingRepository(connection)
            if repository.batch_by_code(payload["code"]):
                raise ConflictError("考试批次编码已存在")
            return repository.create_batch(code=payload["code"], name=payload["name"], created_by=actor, now=now)

    def get_batch_view(self, code: str) -> dict[str, Any]:
        repository = GradingRepository(self.connection)
        batch = repository.batch_by_code(code)
        if batch is None:
            raise NotFoundError("考试批次不存在")
        view = dict(batch)
        view["latest_candidate"] = self._candidate_view(repository, batch["latest_candidate_id"])
        view["published_candidate"] = self._candidate_view(repository, batch["published_candidate_id"])
        view["candidates"] = [
            {key: row[key] for key in ("id", "scorer_code", "scorer_name", "status", "version", "created_at", "published_at")}
            for row in repository.list_candidates(batch["id"])
        ]
        view["publications"] = repository.publications(batch["id"])
        view["transitions"] = repository.transitions(batch["id"])
        return view

    # ------------------------------------------------------------------ 候选

    def submit_candidate(self, code: str, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("grades.compute")
        scores = self._normalize_scores(payload["scores"])
        digest = scores_digest(scores, payload["metrics"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = GradingRepository(connection)
            batch = repository.batch_by_code(code)
            if batch is None:
                raise NotFoundError("考试批次不存在")
            candidate_id = repository.add_candidate(
                batch_id=batch["id"],
                scorer_code=payload["scorer_code"],
                scorer_name=payload["scorer_name"],
                scores=scores,
                metrics=payload["metrics"],
                scores_digest=digest,
                computed_by_id=principal.user_id,
                computed_by=principal.display_name,
                now=now,
            )
            repository.point_latest(batch["id"], candidate_id, now)
            repository.add_transition(
                batch_id=batch["id"], candidate_id=candidate_id, action="submit",
                from_status="", to_status="candidate", actor=principal.display_name,
                detail={"scorer_code": payload["scorer_code"], "student_count": len(scores), "digest": digest}, now=now,
            )
            return self._candidate_view(repository, candidate_id)

    # ------------------------------------------------------------------ 复核

    def review_candidate(self, code: str, candidate_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("grades.review")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = GradingRepository(connection)
            batch, candidate = self._load_pair(repository, code, candidate_id)
            if candidate["computed_by_id"] == principal.user_id:
                raise PermissionDeniedError("参与该结果计算的人员不能复核自己产出的候选")
            self._check_version(candidate, payload.get("expected_version"))
            action = "review" if payload["passed"] else "reject"
            self._ensure_action(candidate["status"], action)
            if payload["passed"]:
                repository.mark_reviewed(candidate_id, payload["note"], principal.display_name, now)
                to_status = "reviewed"
            else:
                repository.mark_rejected(candidate_id, payload["note"], principal.display_name, now)
                to_status = "rejected"
            repository.add_transition(
                batch_id=batch["id"], candidate_id=candidate_id, action=action,
                from_status=candidate["status"], to_status=to_status,
                actor=principal.display_name, reason=payload["note"], now=now,
            )
            return self._candidate_view(repository, candidate_id)

    # ------------------------------------------------------------------ 发布

    def publish_candidate(self, code: str, candidate_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("grades.publish")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = GradingRepository(connection)
            batch, candidate = self._load_pair(repository, code, candidate_id)
            if candidate["computed_by_id"] == principal.user_id:
                raise PermissionDeniedError("参与该结果计算的人员不能发布自己产出的候选")
            self._check_version(candidate, payload.get("expected_version"))
            self._ensure_action(candidate["status"], "publish")

            seq = int(batch["publication_seq"]) + 1
            previous_seq: int | None = None
            previous_id = batch["published_candidate_id"]
            if previous_id is not None:
                previous = repository.candidate_by_id(previous_id)
                if previous is not None and previous["status"] == "published":
                    self._ensure_action(previous["status"], "supersede")
                    repository.mark_superseded(previous_id)
                    repository.add_transition(
                        batch_id=batch["id"], candidate_id=previous_id, action="supersede",
                        from_status="published", to_status="superseded", actor=principal.display_name,
                        reason=payload["reason"], detail={"by_candidate_id": candidate_id}, now=now,
                    )
                last = repository.last_publication(batch["id"])
                previous_seq = int(last["seq"]) if last else None

            repository.mark_published(candidate_id, principal.display_name, now)
            repository.switch_publication(batch["id"], candidate_id, seq, now)
            repository.add_publication(
                batch_id=batch["id"], seq=seq, candidate_id=candidate_id, kind="publish",
                actor=principal.display_name, reason=payload["reason"], source_seq=previous_seq, now=now,
            )
            repository.add_transition(
                batch_id=batch["id"], candidate_id=candidate_id, action="publish",
                from_status="reviewed", to_status="published", actor=principal.display_name,
                reason=payload["reason"], detail={"seq": seq, "previous_candidate_id": previous_id}, now=now,
            )
            return self._candidate_view(repository, candidate_id)

    # ------------------------------------------------------------------ 撤销

    def revoke_published(self, code: str, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("grades.publish")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = GradingRepository(connection)
            batch = repository.batch_by_code(code)
            if batch is None:
                raise NotFoundError("考试批次不存在")
            current_id = batch["published_candidate_id"]
            if current_id is None:
                raise ConflictError("当前没有对外发布的成绩，无需撤销")
            current = repository.candidate_by_id(current_id)
            if current is None or current["status"] != "published":
                raise ConflictError("发布指针与候选状态不一致，拒绝撤销")
            if current["computed_by_id"] == principal.user_id:
                raise PermissionDeniedError("参与该结果计算的人员不能撤销自己产出的候选")
            self._ensure_action(current["status"], "revoke")

            target = repository.rollback_target(batch["id"], current_id)
            repository.mark_revoked(current_id, principal.display_name, payload["reason"], now)
            repository.add_transition(
                batch_id=batch["id"], candidate_id=current_id, action="revoke",
                from_status="published", to_status="revoked", actor=principal.display_name,
                reason=payload["reason"],
                detail={"rollback_to_candidate_id": target["candidate_id"] if target else None},
                now=now,
            )

            if target is not None:
                # 只回到发布链上最近一份仍可用的结果
                fallback = repository.candidate_by_id(target["candidate_id"])
                if fallback is None or fallback["status"] not in {"published", "superseded"}:
                    raise ConflictError("上一份发布结果已不可用，无法回退")
                seq = int(batch["publication_seq"]) + 1
                if fallback["status"] == "superseded":
                    self._ensure_action(fallback["status"], "restore")
                    repository.add_transition(
                        batch_id=batch["id"], candidate_id=fallback["id"], action="restore",
                        from_status="superseded", to_status="published", actor=principal.display_name,
                        reason=payload["reason"], detail={"source_seq": target["seq"]}, now=now,
                    )
                repository.mark_published(fallback["id"], principal.display_name, now)
                repository.switch_publication(batch["id"], fallback["id"], seq, now)
                repository.add_publication(
                    batch_id=batch["id"], seq=seq, candidate_id=fallback["id"], kind="rollback",
                    actor=principal.display_name, reason=payload["reason"], source_seq=int(target["seq"]), now=now,
                )
                outcome = "rolled_back"
                published_id = fallback["id"]
            else:
                repository.clear_publication(batch["id"], now)
                outcome = "withdrawn"
                published_id = None

            return {
                "batch": dict(batch),
                "outcome": outcome,
                "revoked_candidate_id": current_id,
                "published_candidate_id": published_id,
                "published_candidate": self._candidate_view(repository, published_id),
            }

    # ------------------------------------------------------------------ 比较

    def compare(self, code: str, candidate_id: int | None = None, baseline_id: int | None = None) -> dict[str, Any]:
        repository = GradingRepository(self.connection)
        batch = repository.batch_by_code(code)
        if batch is None:
            raise NotFoundError("考试批次不存在")
        candidate_row = self._resolve(repository, batch, candidate_id, batch["latest_candidate_id"], "候选结果")
        baseline_row = (
            self._resolve(repository, batch, baseline_id, batch["published_candidate_id"], "对比基线")
            if baseline_id is not None or batch["published_candidate_id"] is not None
            else None
        )
        candidate = json.loads(candidate_row["scores_json"])
        baseline = json.loads(baseline_row["scores_json"]) if baseline_row is not None else []
        candidate_metrics = json.loads(candidate_row["metrics_json"])
        baseline_metrics = json.loads(baseline_row["metrics_json"]) if baseline_row is not None else {}

        base_by_code = {item["student_code"]: item for item in baseline}
        cand_by_code = {item["student_code"]: item for item in candidate}
        added = sorted(set(cand_by_code) - set(base_by_code))
        removed = sorted(set(base_by_code) - set(cand_by_code))

        base_items = {key for item in baseline for key in item.get("items", {})}
        cand_items = {key for item in candidate for key in item.get("items", {})}

        student_changes: list[dict[str, Any]] = []
        for student_code in sorted(set(base_by_code) & set(cand_by_code)):
            old = base_by_code[student_code]
            new = cand_by_code[student_code]
            old_items = old.get("items", {})
            new_items = new.get("items", {})
            item_deltas: dict[str, dict[str, Any]] = {}
            for item_key in sorted(set(old_items) & set(new_items)):
                if _is_number(old_items[item_key]) and _is_number(new_items[item_key]):
                    item_deltas[item_key] = {
                        "from": old_items[item_key], "to": new_items[item_key],
                        "delta": round(new_items[item_key] - old_items[item_key], 6),
                    }
            change = {
                "student_code": student_code,
                "student_name": new.get("student_name", old.get("student_name", "")),
                "total_delta": round(new["total"] - old["total"], 6) if _is_number(old["total"]) and _is_number(new["total"]) else None,
                "items_added": sorted(set(new_items) - set(old_items)),
                "items_removed": sorted(set(old_items) - set(new_items)),
                "item_deltas": item_deltas,
            }
            changed = (
                change["total_delta"] not in (None, 0)
                or change["items_added"] or change["items_removed"]
                or any(detail["delta"] != 0 for detail in item_deltas.values())
            )
            if changed:
                student_changes.append(change)

        metric_deltas: dict[str, dict[str, Any]] = {}
        metric_changes: dict[str, dict[str, Any]] = {}
        for key in sorted(set(baseline_metrics) | set(candidate_metrics)):
            old_value = baseline_metrics.get(key)
            new_value = candidate_metrics.get(key)
            if old_value == new_value:
                continue
            if _is_number(old_value) and _is_number(new_value):
                metric_deltas[key] = {"from": old_value, "to": new_value, "delta": round(new_value - old_value, 6)}
            else:
                metric_changes[key] = {"from": old_value, "to": new_value}

        structure = {
            "students_added": added,
            "students_removed": removed,
            "items_added": sorted(cand_items - base_items),
            "items_removed": sorted(base_items - cand_items),
        }
        identical = not (added or removed or student_changes or metric_deltas or metric_changes
                         or structure["items_added"] or structure["items_removed"])
        return {
            "batch_code": batch["code"],
            "baseline": self._candidate_summary(baseline_row) if baseline_row is not None else None,
            "candidate": self._candidate_summary(candidate_row),
            "metrics_delta": metric_deltas,
            "metric_changes": metric_changes,
            "structure": structure,
            "student_changes": student_changes,
            "identical": identical,
        }

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _normalize_scores(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scores = [dict(item) for item in raw]
        codes = [item["student_code"] for item in scores]
        if len(set(codes)) != len(codes):
            raise ValidationError("同一候选内学号不能重复", context={"student_codes": sorted({c for c in codes if codes.count(c) > 1})})
        for item in scores:
            if not _is_number(item["total"]):
                raise ValidationError(f"学生 {item['student_code']} 的总分必须是数值")
            for name, value in item.get("items", {}).items():
                if not _is_number(value):
                    raise ValidationError(f"学生 {item['student_code']} 的分项 {name} 必须是数值")
        return scores

    @staticmethod
    def _load_pair(repository: GradingRepository, code: str, candidate_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
        batch = repository.batch_by_code(code)
        if batch is None:
            raise NotFoundError("考试批次不存在")
        candidate = repository.candidate_by_id(candidate_id)
        if candidate is None or candidate["batch_id"] != batch["id"]:
            raise NotFoundError("候选结果不存在或不属于该考试批次")
        return batch, candidate

    @staticmethod
    def _resolve(repository: GradingRepository, batch: sqlite3.Row, explicit_id: int | None,
                 default_id: int | None, label: str) -> sqlite3.Row:
        target_id = explicit_id if explicit_id is not None else default_id
        if target_id is None:
            raise ConflictError(f"{label}不存在，无法比较")
        row = repository.candidate_by_id(target_id)
        if row is None or row["batch_id"] != batch["id"]:
            raise NotFoundError(f"{label}不存在或不属于该考试批次")
        return row

    @staticmethod
    def _ensure_action(status: str, action: str) -> None:
        if action not in TRANSITIONS.get(status, set()):
            raise ConflictError(f"候选当前状态为 {status}，不允许执行 {action}；该请求可能是迟到的审批")

    @staticmethod
    def _check_version(candidate: sqlite3.Row, expected_version: int | None) -> None:
        if expected_version is not None and int(candidate["version"]) != int(expected_version):
            raise ConflictError(
                "候选版本已变化，拒绝基于过期状态的操作",
                context={"current_version": candidate["version"], "expected_version": expected_version},
            )

    @staticmethod
    def _candidate_view(repository: GradingRepository, candidate_id: int | None) -> dict[str, Any] | None:
        if candidate_id is None:
            return None
        row = repository.candidate_by_id(candidate_id)
        if row is None:
            return None
        view = dict(row)
        view["scores"] = json.loads(row["scores_json"])
        view["metrics"] = json.loads(row["metrics_json"])
        return view

    @staticmethod
    def _candidate_summary(row: sqlite3.Row) -> dict[str, Any]:
        scores = json.loads(row["scores_json"])
        return {
            "id": row["id"],
            "scorer_code": row["scorer_code"],
            "scorer_name": row["scorer_name"],
            "status": row["status"],
            "version": row["version"],
            "student_count": len(scores),
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "digest": row["scores_digest"],
        }
