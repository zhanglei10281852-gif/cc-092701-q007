from __future__ import annotations

import json
import sqlite3
from typing import Any

# 候选状态允许的转换动作；每个动作只在单一前置状态下合法，
# 保证迟到的复核/发布审批不可能覆盖后来的决定。
TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"review", "reject"},
    "reviewed": {"publish"},
    "published": {"supersede", "revoke"},
    "superseded": {"revoke", "restore"},
    "rejected": set(),
    "revoked": set(),
}


class GradingRepository:
    """成绩治理域的 SQLite 读写，所有方法假定调用方持有事务连接。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def batch_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM grading_batches WHERE code=?", (code,)).fetchone()

    def batch_by_id(self, batch_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM grading_batches WHERE id=?", (batch_id,)).fetchone()

    def create_batch(self, *, code: str, name: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO grading_batches(code,name,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
            (code, name, created_by, now, now),
        )
        batch_id = int(cursor.lastrowid)
        return dict(self.batch_by_id(batch_id))

    def candidate_by_id(self, candidate_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM grading_candidates WHERE id=?", (candidate_id,)).fetchone()

    def latest_candidate(self, batch_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM grading_candidates WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)
        ).fetchone()

    def list_candidates(self, batch_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM grading_candidates WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]

    def add_candidate(
        self,
        *,
        batch_id: int,
        scorer_code: str,
        scorer_name: str,
        scores: list[dict[str, Any]],
        metrics: dict[str, Any],
        scores_digest: str,
        computed_by_id: int,
        computed_by: str,
        now: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO grading_candidates(batch_id,scorer_code,scorer_name,scores_json,metrics_json,"
            "scores_digest,status,computed_by_id,computed_by,created_at) VALUES(?,?,?,?,?,?,'candidate',?,?,?)",
            (
                batch_id,
                scorer_code,
                scorer_name,
                json.dumps(scores, ensure_ascii=False, sort_keys=True),
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                scores_digest,
                computed_by_id,
                computed_by,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def point_latest(self, batch_id: int, candidate_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE grading_batches SET latest_candidate_id=?,updated_at=? WHERE id=?", (candidate_id, now, batch_id)
        )

    def mark_reviewed(self, candidate_id: int, note: str, reviewer: str, now: str) -> None:
        self.connection.execute(
            "UPDATE grading_candidates SET status='reviewed',version=version+1,review_note=?,"
            "reviewed_by=?,reviewed_at=? WHERE id=?",
            (note, reviewer, now, candidate_id),
        )

    def mark_rejected(self, candidate_id: int, note: str, reviewer: str, now: str) -> None:
        self.connection.execute(
            "UPDATE grading_candidates SET status='rejected',version=version+1,review_note=?,"
            "reviewed_by=?,reviewed_at=? WHERE id=?",
            (note, reviewer, now, candidate_id),
        )

    def mark_published(self, candidate_id: int, publisher: str, now: str) -> None:
        self.connection.execute(
            "UPDATE grading_candidates SET status='published',version=version+1,"
            "published_by=?,published_at=? WHERE id=?",
            (publisher, now, candidate_id),
        )

    def mark_superseded(self, candidate_id: int) -> None:
        self.connection.execute("UPDATE grading_candidates SET status='superseded' WHERE id=?", (candidate_id,))

    def mark_revoked(self, candidate_id: int, actor: str, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE grading_candidates SET status='revoked',version=version+1,revoked_by=?,"
            "revoked_at=?,revoke_reason=? WHERE id=?",
            (actor, now, reason, candidate_id),
        )

    def switch_publication(self, batch_id: int, candidate_id: int, seq: int, now: str) -> None:
        """在同一事务内切换批次对外发布的当前指针。"""
        self.connection.execute(
            "UPDATE grading_batches SET published_candidate_id=?,publication_seq=?,updated_at=? WHERE id=?",
            (candidate_id, seq, now, batch_id),
        )

    def clear_publication(self, batch_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE grading_batches SET published_candidate_id=NULL,updated_at=? WHERE id=?", (now, batch_id)
        )

    def add_publication(
        self,
        *,
        batch_id: int,
        seq: int,
        candidate_id: int,
        kind: str,
        actor: str,
        reason: str,
        source_seq: int | None,
        now: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO grading_publications(batch_id,seq,candidate_id,kind,actor,reason,source_seq,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (batch_id, seq, candidate_id, kind, actor, reason, source_seq, now),
        )

    def last_publication(self, batch_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM grading_publications WHERE batch_id=? ORDER BY seq DESC,id DESC LIMIT 1", (batch_id,)
        ).fetchone()

    def publications(self, batch_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM grading_publications WHERE batch_id=? ORDER BY seq,id", (batch_id,)
            ).fetchall()
        ]

    def rollback_target(self, batch_id: int, current_candidate_id: int) -> sqlite3.Row | None:
        """上一份仍可回退的结果：发布链上最近一条指向其他候选的记录，
        且该候选当前仍处于 published/superseded（未被撤销）。"""
        rows = self.connection.execute(
            "SELECT p.* FROM grading_publications p JOIN grading_candidates c ON c.id=p.candidate_id "
            "WHERE p.batch_id=? AND p.candidate_id<>? AND c.status IN ('published','superseded') "
            "ORDER BY p.seq DESC,p.id DESC LIMIT 1",
            (batch_id, current_candidate_id),
        ).fetchall()
        return rows[0] if rows else None

    def add_transition(
        self,
        *,
        batch_id: int,
        candidate_id: int,
        action: str,
        from_status: str,
        to_status: str,
        actor: str,
        reason: str = "",
        detail: dict[str, Any] | None = None,
        now: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO grading_transitions(batch_id,candidate_id,action,from_status,to_status,actor,reason,"
            "detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                candidate_id,
                action,
                from_status,
                to_status,
                actor,
                reason,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def transitions(self, batch_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM grading_transitions WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        ]
