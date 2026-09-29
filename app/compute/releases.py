from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import Principal
from app.database import get_connection, transaction

CANDIDATE = "candidate"
IN_REVIEW = "in_review"
APPROVED = "approved"
PUBLISHED = "published"
REJECTED = "rejected"
REVOKED = "revoked"
SUPERSEDED = "superseded"

# 曾经发布、仍可作为撤销回退目标的状态
USABLE_PRIOR_STATES = (SUPERSEDED,)


class ReleaseRepository:
    """结果发布候选与审计事件的读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def task_exists(self, task_id: int) -> bool:
        return self.connection.execute("SELECT 1 FROM compute_tasks WHERE id=?", (task_id,)).fetchone() is not None

    def result_by_version(self, task_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_results WHERE task_id=? AND version=?", (task_id, version)
        ).fetchone()

    def latest_result(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_results WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,)
        ).fetchone()

    def release_by_id(self, release_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_result_releases WHERE id=?", (release_id,)).fetchone()

    def release_for_result(self, task_id: int, result_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_result_releases WHERE task_id=? AND result_id=? AND status<>? ORDER BY sequence DESC LIMIT 1",
            (task_id, result_id, REJECTED),
        ).fetchone()

    def list_releases(self, task_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM compute_result_releases WHERE task_id=? ORDER BY sequence", (task_id,)
        ).fetchall()

    def published_release(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_result_releases WHERE task_id=? AND status=?", (task_id, PUBLISHED)
        ).fetchone()

    def prior_usable_release(self, task_id: int, before_sequence: int) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in USABLE_PRIOR_STATES)
        return self.connection.execute(
            f"SELECT * FROM compute_result_releases WHERE task_id=? AND sequence<? AND status IN ({placeholders}) "
            "ORDER BY sequence DESC LIMIT 1",
            (task_id, before_sequence, *USABLE_PRIOR_STATES),
        ).fetchone()

    def next_sequence(self, task_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM compute_result_releases WHERE task_id=?", (task_id,)
        ).fetchone()[0])

    def insert_release(self, *, task_id: int, result_id: int, sequence: int, scorer_code: str,
                       computed_by: str, submitted_by: str, note: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO compute_result_releases(task_id,result_id,sequence,scorer_code,status,submitted_by,"
            "submitted_at,review_note,computed_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, result_id, sequence, scorer_code, CANDIDATE, submitted_by, now, note, computed_by),
        )
        return self.release_by_id(int(cursor.lastrowid))  # type: ignore[return-value]

    def add_event(self, *, release_id: int, task_id: int, action: str, actor: Principal,
                  note: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_result_release_events(release_id,task_id,action,actor,actor_user_id,note,"
            "before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (release_id, task_id, action, actor.username, actor.user_id, note,
             json.dumps(before, ensure_ascii=False, sort_keys=True),
             json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM compute_result_release_events WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()]


class ResultReleaseService:
    """候选、复核、发布与撤销之间的可审计状态转换。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()

    def submit_candidate(self, task_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ReleaseRepository(connection)
            if not repository.task_exists(task_id):
                raise NotFoundError("计算任务不存在")
            result = repository.result_by_version(task_id, int(payload["result_version"]))
            if result is None:
                raise NotFoundError("该任务不存在对应版本的计算结果")
            if repository.release_for_result(task_id, int(result["id"])) is not None:
                raise ConflictError("该结果版本已有候选或发布记录，被驳回后才能重新提交")
            sequence = repository.next_sequence(task_id)
            release = repository.insert_release(
                task_id=task_id, result_id=int(result["id"]), sequence=sequence,
                scorer_code=payload["scorer_code"], computed_by=result["created_by"],
                submitted_by=principal.username, note=payload.get("note", ""), now=now,
            )
            after = dict(release)
            repository.add_event(release_id=int(release["id"]), task_id=task_id, action="submit",
                                 actor=principal, note=payload.get("note", ""), before={}, after=after, now=now)
            return self._view(connection, release)

    def start_review(self, release_id: int, principal: Principal, note: str = "") -> dict[str, Any]:
        return self._transition(
            release_id, principal, "start_review", "compute.review",
            allowed_from={CANDIDATE}, to_status=IN_REVIEW, note=note, require_note=False,
        )

    def approve(self, release_id: int, principal: Principal, note: str) -> dict[str, Any]:
        return self._transition(
            release_id, principal, "approve", "compute.review",
            allowed_from={IN_REVIEW}, to_status=APPROVED, note=note,
            require_independent=True,
        )

    def reject(self, release_id: int, principal: Principal, note: str) -> dict[str, Any]:
        return self._transition(
            release_id, principal, "reject", "compute.review",
            allowed_from={CANDIDATE, IN_REVIEW}, to_status=REJECTED, note=note,
            require_independent=True,
        )

    def publish(self, release_id: int, principal: Principal, note: str) -> dict[str, Any]:
        principal.require("compute.publish")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ReleaseRepository(connection)
            release = self._require_release(repository, release_id)
            before = dict(release)
            if release["status"] != APPROVED:
                raise ConflictError("只有复核通过的候选可以发布", context={"current_status": release["status"]})
            self._require_independent(release, principal)
            current = repository.published_release(int(release["task_id"]))
            if current is not None and int(current["id"]) != int(release["id"]):
                current_before = dict(current)
                connection.execute(
                    "UPDATE compute_result_releases SET status=?,superseded_by_release_id=? WHERE id=?",
                    (SUPERSEDED, release["id"], current["id"]),
                )
                current_after = dict(repository.release_by_id(int(current["id"])))
                repository.add_event(release_id=int(current["id"]), task_id=int(release["task_id"]),
                                     action="supersede", actor=principal, note=note,
                                     before=current_before, after=current_after, now=now)
            connection.execute(
                "UPDATE compute_result_releases SET status=?,published_by=?,published_at=? WHERE id=?",
                (PUBLISHED, principal.username, now, release_id),
            )
            connection.execute(
                "UPDATE compute_tasks SET published_release_id=? WHERE id=?",
                (release_id, release["task_id"]),
            )
            after = dict(repository.release_by_id(release_id))
            repository.add_event(release_id=release_id, task_id=int(release["task_id"]), action="publish",
                                 actor=principal, note=note, before=before, after=after, now=now)
            return self._view(connection, repository.release_by_id(release_id))  # type: ignore[arg-type]

    def revoke(self, release_id: int, principal: Principal, reason: str) -> dict[str, Any]:
        principal.require("compute.revoke")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ReleaseRepository(connection)
            release = self._require_release(repository, release_id)
            task_id = int(release["task_id"])
            before = dict(release)
            if release["status"] != PUBLISHED:
                raise ConflictError("只能撤销当前已发布的结果", context={"current_status": release["status"]})
            connection.execute(
                "UPDATE compute_result_releases SET status=?,revoked_by=?,revoked_at=?,revoke_reason=? WHERE id=?",
                (REVOKED, principal.username, now, reason, release_id),
            )
            after = dict(repository.release_by_id(release_id))
            repository.add_event(release_id=release_id, task_id=task_id, action="revoke",
                                 actor=principal, note=reason, before=before, after=after, now=now)
            prior = repository.prior_usable_release(task_id, int(release["sequence"]))
            reinstated_view = None
            if prior is not None:
                prior_before = dict(prior)
                connection.execute(
                    "UPDATE compute_result_releases SET status=? WHERE id=?", (PUBLISHED, prior["id"])
                )
                connection.execute(
                    "UPDATE compute_tasks SET published_release_id=? WHERE id=?", (prior["id"], task_id)
                )
                prior_after = dict(repository.release_by_id(int(prior["id"])))
                repository.add_event(release_id=int(prior["id"]), task_id=task_id, action="reinstate",
                                     actor=principal, note=reason, before=prior_before, after=prior_after, now=now)
                reinstated_view = self._view(connection, prior_after)
            else:
                connection.execute("UPDATE compute_tasks SET published_release_id=NULL WHERE id=?", (task_id,))
            view = self._view(connection, after)
            view["reinstated_release"] = reinstated_view
            return view

    def overview(self, task_id: int) -> dict[str, Any]:
        with transaction() as connection:
            repository = ReleaseRepository(connection)
            if not repository.task_exists(task_id):
                raise NotFoundError("计算任务不存在")
            latest = repository.latest_result(task_id)
            published = repository.published_release(task_id)
            releases = [self._view(connection, row) for row in repository.list_releases(task_id)]
            return {
                "task_id": task_id,
                "latest_result": self._result_summary(latest),
                "current_published": self._view(connection, published) if published else None,
                "releases": releases,
                "events": repository.events(task_id),
            }

    def compare(self, task_id: int, base_sequence: int, target_sequence: int) -> dict[str, Any]:
        with transaction() as connection:
            repository = ReleaseRepository(connection)
            base_row = self._release_by_sequence(repository, task_id, base_sequence)
            target_row = self._release_by_sequence(repository, task_id, target_sequence)
            base_payload = self._result_payload(repository, base_row)
            target_payload = self._result_payload(repository, target_row)
            metrics = _diff_mapping(base_payload["metrics"], target_payload["metrics"])
            structure = _diff_structure(base_payload["result"], target_payload["result"])
            return {
                "task_id": task_id,
                "base": {"release_id": base_row["id"], "sequence": base_row["sequence"],
                         "result_version": base_payload["version"], "scorer_code": base_row["scorer_code"],
                         "status": base_row["status"]},
                "target": {"release_id": target_row["id"], "sequence": target_row["sequence"],
                           "result_version": target_payload["version"], "scorer_code": target_row["scorer_code"],
                           "status": target_row["status"]},
                "metric_differences": metrics,
                "structural_changes": structure,
            }

    # -- 内部辅助 -------------------------------------------------------------

    def _transition(self, release_id: int, principal: Principal, action: str, permission: str, *,
                    allowed_from: set[str], to_status: str, note: str,
                    require_independent: bool = False, require_note: bool = True) -> dict[str, Any]:
        principal.require(permission)
        if require_note and not note.strip():
            from app.core.errors import ValidationError
            raise ValidationError("复核意见不能为空")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ReleaseRepository(connection)
            release = self._require_release(repository, release_id)
            before = dict(release)
            if release["status"] not in allowed_from:
                # 迟到的审批不能覆盖已经发生的后续决定
                raise ConflictError(
                    f"当前状态 {release['status']} 不允许执行 {action}",
                    context={"current_status": release["status"], "expected": sorted(allowed_from)},
                )
            if require_independent:
                self._require_independent(release, principal)
            fields: dict[str, str] = {"status": to_status}
            if action == "start_review":
                fields["review_started_by"] = principal.username
                fields["review_started_at"] = now
            else:
                fields["reviewed_by"] = principal.username
                fields["reviewed_at"] = now
                fields["review_note"] = note
            assignments = ",".join(f"{key}=?" for key in fields)
            connection.execute(
                f"UPDATE compute_result_releases SET {assignments} WHERE id=?",
                (*fields.values(), release_id),
            )
            after = dict(repository.release_by_id(release_id))
            repository.add_event(release_id=release_id, task_id=int(release["task_id"]), action=action,
                                 actor=principal, note=note, before=before, after=after, now=now)
            return self._view(connection, after)

    @staticmethod
    def _require_release(repository: ReleaseRepository, release_id: int) -> sqlite3.Row:
        release = repository.release_by_id(release_id)
        if release is None:
            raise NotFoundError("结果发布候选不存在")
        return release

    @staticmethod
    def _release_by_sequence(repository: ReleaseRepository, task_id: int, sequence: int) -> sqlite3.Row:
        for row in repository.list_releases(task_id):
            if int(row["sequence"]) == sequence:
                return row
        raise NotFoundError(f"候选序号 {sequence} 不存在")

    @staticmethod
    def _require_independent(release: sqlite3.Row, principal: Principal) -> None:
        participants = {release["submitted_by"], release["computed_by"]}
        if principal.username in participants:
            raise PermissionDeniedError("参与过该结果计算或提交的人员不能复核、发布该结果")

    @staticmethod
    def _result_payload(repository: ReleaseRepository, release: sqlite3.Row) -> dict[str, Any]:
        row = repository.connection.execute(
            "SELECT * FROM compute_results WHERE id=?", (release["result_id"],)
        ).fetchone()
        if row is None:
            raise NotFoundError("候选关联的计算结果已不存在")
        return {
            "version": row["version"],
            "result": json.loads(row["result_json"]),
            "metrics": json.loads(row["metrics_json"]),
        }

    @staticmethod
    def _result_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "version": row["version"], "created_by": row["created_by"],
            "created_at": row["created_at"], "result_digest": row["result_digest"],
            "metrics": json.loads(row["metrics_json"]),
        }

    def _view(self, connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        result = connection.execute("SELECT version,metrics_json,result_digest FROM compute_results WHERE id=?",
                                    (item["result_id"],)).fetchone()
        if result is not None:
            item["result_version"] = result["version"]
            item["metrics"] = json.loads(result["metrics_json"])
            item["result_digest"] = result["result_digest"]
        return item


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _diff_mapping(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    added = sorted(set(target) - set(base))
    removed = sorted(set(base) - set(target))
    changed: list[dict[str, Any]] = []
    for key in sorted(set(base) & set(target)):
        if base[key] != target[key]:
            entry: dict[str, Any] = {"key": key, "base": base[key], "target": target[key]}
            if _is_number(base[key]) and _is_number(target[key]):
                entry["delta"] = target[key] - base[key]
            changed.append(entry)
    return {"added": added, "removed": removed, "changed": changed}


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """返回 路径 -> 结构类型；标量路径保留值类型，数组整体作为 list 节点。"""
    if isinstance(value, dict):
        paths: dict[str, str] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_flatten(value[key], child))
        return paths
    if isinstance(value, list):
        return {prefix: f"list[{len(value)}]"}
    if isinstance(value, bool):
        return {prefix: "boolean"}
    if isinstance(value, int):
        return {prefix: "integer"}
    if isinstance(value, float):
        return {prefix: "number"}
    if value is None:
        return {prefix: "null"}
    return {prefix: type(value).__name__}


def _leaf_at(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        current = current[part]
    return current


def _diff_structure(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    base_paths = _flatten(base)
    target_paths = _flatten(target)
    added = sorted(set(target_paths) - set(base_paths))
    removed = sorted(set(base_paths) - set(target_paths))
    changed: list[dict[str, Any]] = []
    for path in sorted(set(base_paths) & set(target_paths)):
        base_type = base_paths[path]
        target_type = target_paths[path]
        base_value = _leaf_at(base, path)
        target_value = _leaf_at(target, path)
        if base_type != target_type:
            changed.append({"path": path, "change": "type", "base_type": base_type,
                            "target_type": target_type, "base": base_value, "target": target_value})
        elif base_value != target_value:
            changed.append({"path": path, "change": "value", "base_type": base_type,
                            "base": base_value, "target": target_value})
    return {"added_paths": added, "removed_paths": removed, "changed_paths": changed}
