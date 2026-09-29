from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_compute_operations import TEMPLATE, create_template, submit_payload


def _bootstrap(client: TestClient, username: str, role_codes: list[str] | None = None) -> dict:
    response = client.post(
        "/api/users",
        json={
            "username": username,
            "password": "Passw0rd!abc",
            "display_name": username,
            "role_codes": role_codes or [],
        },
        headers=_admin_headers(client),
    )
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Passw0rd!abc", "client_label": "t"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "username": username}


def _admin_headers(client: TestClient) -> dict:
    client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "t"})
    login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "t"})
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _grant_auditor_release_perms(client: TestClient, admin_headers: dict) -> None:
    role = client.get("/api/roles", headers=admin_headers).json()
    auditor_id = next(item["id"] for item in role if item["code"] == "auditor")
    perms = client.get("/api/roles/permissions", headers=admin_headers).json()
    codes = [p["code"] for p in perms if p["code"].startswith("compute.")]
    assert set(codes) == {"compute.review", "compute.publish", "compute.revoke"}
    response = client.patch(f"/api/roles/{auditor_id}",
                            json={"permission_codes": ["audit.read", *codes]}, headers=admin_headers)
    assert response.status_code == 200, response.text


def _prepare_two_scorers(client: TestClient, admin_headers: dict) -> int:
    """同一批任务由两套评分器产出 v1/v2 两个结果版本，返回 task_id。"""
    create_template(client)
    task = client.post("/api/compute/tasks", json=submit_payload("exam-batch-001")).json()
    task_id = task["id"]
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "scorera", "capabilities": ["solver-a"], "lease_seconds": 60})
    assert claimed.json()["task"]["id"] == task_id
    first = client.post(
        f"/api/compute/tasks/{task_id}/complete",
        json={"worker_id": "scorera", "result": {"grade": "良", "score": 80, "detail": {"theory": 78, "practice": 82}},
              "metrics": {"pass_rate": 0.9, "average": 79.5}},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/compute/tasks/{task_id}/rescore",
        json={"worker_id": "scorerb", "scorer_code": "scorer-b-v2",
              "result": {"grade": "优", "score": 91, "detail": {"theory": 90, "practice": 92, "extra": 1}},
              "metrics": {"pass_rate": 0.97, "average": 91.0, "duration": 12}},
    )
    assert second.status_code == 200, second.text
    return task_id


def test_full_candidate_review_publish_flow_and_permissions(client):
    admin_headers = _admin_headers(client)
    _grant_auditor_release_perms(client, admin_headers)
    reviewer = _bootstrap(client, "reviewer1", ["auditor"])

    task_id = _prepare_two_scorers(client, admin_headers)

    # 提交两个候选（v1 与 v2 各一套评分器）
    c1 = client.post(f"/api/compute/tasks/{task_id}/releases",
                     json={"result_version": 1, "scorer_code": "scorer-a-v1", "note": "甲评分器结果"},
                     headers=admin_headers)
    assert c1.status_code == 201, c1.text
    assert c1.json()["status"] == "candidate" and c1.json()["sequence"] == 1
    c2 = client.post(f"/api/compute/tasks/{task_id}/releases",
                     json={"result_version": 2, "scorer_code": "scorer-b-v2", "note": "乙评分器结果"},
                     headers=admin_headers)
    assert c2.status_code == 201 and c2.json()["sequence"] == 2

    # 无权限不能进入复核
    nobody = _bootstrap(client, "nobody")
    forbidden = client.post(f"/api/compute/releases/{c1.json()['id']}/review-start", json={"note": ""}, headers=nobody["headers"])
    assert forbidden.status_code == 403

    # 比较：指标差异与结构变化
    comparison = client.get(f"/api/compute/tasks/{task_id}/releases/compare?base=1&target=2", headers=reviewer["headers"])
    assert comparison.status_code == 200, comparison.text
    body = comparison.json()
    changed_metrics = {item["key"]: item["delta"] for item in body["metric_differences"]["changed"]}
    assert set(changed_metrics) == {"pass_rate", "average"}
    assert abs(changed_metrics["average"] - 11.5) < 1e-9
    assert body["metric_differences"]["added"] == ["duration"]
    assert body["structural_changes"]["added_paths"] == ["detail.extra"]
    changed_paths = {item["path"] for item in body["structural_changes"]["changed_paths"]}
    assert {"grade", "score", "detail.theory", "detail.practice"} <= changed_paths

    # 参与计算的人不能发布自己的结果（scorera 是 v1 的计算者，即使有权限也被回避规则拒绝）
    scorer_user = _bootstrap(client, "scorera", ["auditor"])
    client.post(f"/api/compute/releases/{c1.json()['id']}/review-start", json={}, headers=reviewer["headers"])
    self_approve = client.post(f"/api/compute/releases/{c1.json()['id']}/approve",
                               json={"note": "我自己算的没问题"}, headers=scorer_user["headers"])
    assert self_approve.status_code == 403

    # 未开始复核不能批准
    early = client.post(f"/api/compute/releases/{c2.json()['id']}/approve",
                        json={"note": "直接批准"}, headers=reviewer["headers"])
    assert early.status_code == 409

    # 正式批准 c1
    approved = client.post(f"/api/compute/releases/{c1.json()['id']}/approve",
                           json={"note": "复核无误，同意发布"}, headers=reviewer["headers"])
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    # 事务内发布：c1 成为当前发布
    published = client.post(f"/api/compute/releases/{c1.json()['id']}/publish",
                            json={"note": "对外发布甲评分器结果"}, headers=reviewer["headers"])
    assert published.status_code == 200 and published.json()["status"] == "published"
    details = client.get(f"/api/compute/task-details/{task_id}").json()
    assert details["published_release_id"] == c1.json()["id"]
    overview = details["release_overview"]
    assert overview["current_published"]["sequence"] == 1
    assert overview["latest_result"]["version"] == 2  # 最近计算是 v2，当前发布是 v1，二者并列呈现

    # 发布 c2：旧发布被取代但记录保留
    client.post(f"/api/compute/releases/{c2.json()['id']}/review-start", json={}, headers=reviewer["headers"])
    client.post(f"/api/compute/releases/{c2.json()['id']}/approve", json={"note": "乙结果更优"}, headers=reviewer["headers"])
    republished = client.post(f"/api/compute/releases/{c2.json()['id']}/publish",
                              json={"note": "改用乙评分器"}, headers=reviewer["headers"])
    assert republished.status_code == 200
    overview = client.get(f"/api/compute/tasks/{task_id}/releases", headers=reviewer["headers"]).json()
    statuses = {item["sequence"]: item["status"] for item in overview["releases"]}
    assert statuses == {1: "superseded", 2: "published"}
    assert overview["current_published"]["sequence"] == 2

    # 迟到的审批不能覆盖后来的决定：c1 已被取代，再批准/发布均失败
    late = client.post(f"/api/compute/releases/{c1.json()['id']}/publish",
                       json={"note": "迟到的发布"}, headers=reviewer["headers"])
    assert late.status_code == 409
    late_approve = client.post(f"/api/compute/releases/{c1.json()['id']}/approve",
                               json={"note": "迟到的批准"}, headers=reviewer["headers"])
    assert late_approve.status_code == 409

    # 撤销 c2：只能回到上一份仍可用（superseded）的 c1，记录不删除
    revoked = client.post(f"/api/compute/releases/{c2.json()['id']}/revoke",
                          json={"reason": "发现评分偏差，撤回乙结果"}, headers=reviewer["headers"])
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["status"] == "revoked"
    assert body["reinstated_release"]["sequence"] == 1
    overview = client.get(f"/api/compute/tasks/{task_id}/releases", headers=reviewer["headers"]).json()
    statuses = {item["sequence"]: item["status"] for item in overview["releases"]}
    assert statuses == {1: "published", 2: "revoked"}
    assert overview["current_published"]["sequence"] == 1
    details = client.get(f"/api/compute/task-details/{task_id}").json()
    assert details["published_release_id"] == c1.json()["id"]

    # 再次撤销 c1：没有更早的可用结果，指针清空；旧记录仍在
    revoked_again = client.post(f"/api/compute/releases/{c1.json()['id']}/revoke",
                                json={"reason": "全部撤回待重算"}, headers=reviewer["headers"])
    assert revoked_again.status_code == 200
    assert revoked_again.json()["reinstated_release"] is None
    overview = client.get(f"/api/compute/tasks/{task_id}/releases", headers=reviewer["headers"]).json()
    assert overview["current_published"] is None
    assert len(overview["releases"]) == 2
    actions = [event["action"] for event in overview["events"]]
    assert actions == ["submit", "submit", "start_review", "approve", "publish",
                       "start_review", "approve", "supersede", "publish",
                       "revoke", "reinstate", "revoke"]
    # 已撤销的不能重复撤销
    duplicate = client.post(f"/api/compute/releases/{c1.json()['id']}/revoke",
                            json={"reason": "再次撤销"}, headers=reviewer["headers"])
    assert duplicate.status_code == 409


def test_rejected_result_can_be_resubmitted_and_compare_requires_existence(client):
    admin_headers = _admin_headers(client)
    _grant_auditor_release_perms(client, admin_headers)
    reviewer = _bootstrap(client, "rev1", ["auditor"])

    task_id = _prepare_two_scorers(client, admin_headers)
    c1 = client.post(f"/api/compute/tasks/{task_id}/releases",
                     json={"result_version": 1, "scorer_code": "a", "note": "x"}, headers=admin_headers).json()
    # 同一结果重复提交被拒绝
    duplicate = client.post(f"/api/compute/tasks/{task_id}/releases",
                            json={"result_version": 1, "scorer_code": "a"}, headers=admin_headers)
    assert duplicate.status_code == 409
    # 不存在的结果版本
    missing = client.post(f"/api/compute/tasks/{task_id}/releases",
                          json={"result_version": 9, "scorer_code": "a"}, headers=admin_headers)
    assert missing.status_code == 404

    client.post(f"/api/compute/releases/{c1['id']}/review-start", json={}, headers=reviewer["headers"])
    rejected = client.post(f"/api/compute/releases/{c1['id']}/reject",
                           json={"note": "指标异常，驳回"}, headers=reviewer["headers"])
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    # 驳回后允许重新提交同一结果
    again = client.post(f"/api/compute/tasks/{task_id}/releases",
                        json={"result_version": 1, "scorer_code": "a-fixed", "note": "重新提交"}, headers=admin_headers)
    assert again.status_code == 201 and again.json()["sequence"] == 2

    # 比较不存在的候选序号
    bad = client.get(f"/api/compute/tasks/{task_id}/releases/compare?base=1&target=99", headers=reviewer["headers"])
    assert bad.status_code == 404
