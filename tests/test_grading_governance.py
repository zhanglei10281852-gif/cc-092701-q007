from __future__ import annotations


def _token(client, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password, "client_label": "t"})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_user(client, admin_headers: dict, username: str, roles: list[str]) -> str:
    response = client.post(
        "/api/users",
        json={"username": username, "password": "Passw0rd!23", "display_name": username, "role_codes": roles},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return _token(client, username, "Passw0rd!23")


def _scores(total_a: float = 80.0, total_b: float = 90.0, *, include_c: bool = False) -> list[dict]:
    items = [
        {"student_code": "S1", "student_name": "张三", "total": total_a, "items": {"written": total_a - 30, "practical": 30}},
        {"student_code": "S2", "student_name": "李四", "total": total_b, "items": {"written": total_b - 30, "practical": 30}},
    ]
    if include_c:
        items.append({"student_code": "S3", "student_name": "王五", "total": 70.0, "items": {"written": 40.0, "practical": 30.0}})
    return items


def _setup_users(client, admin: dict) -> dict[str, str]:
    return {
        "scorer1": _make_user(client, admin["headers"], "scorer1", ["scorer"]),
        "scorer2": _make_user(client, admin["headers"], "scorer2", ["scorer"]),
        "director": _make_user(client, admin["headers"], "director1", ["grades_director"]),
        "admin_token": admin["token"],
    }


def _create_batch(client, headers: dict) -> None:
    response = client.post("/api/grading/batches", json={"code": "skill-2026", "name": "2026 技能考试"}, headers=headers)
    assert response.status_code == 201, response.text


def _submit(client, token: str, scorer_code: str, scores: list[dict], metrics: dict) -> dict:
    response = client.post(
        "/api/grading/batches/skill-2026/candidates",
        json={"scorer_code": scorer_code, "scorer_name": scorer_code, "scores": scores, "metrics": metrics},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _review(client, token: str, candidate_id: int, *, passed: bool, note: str = "ok", expected_version=None) -> int:
    payload = {"passed": passed, "note": note}
    if expected_version is not None:
        payload["expected_version"] = expected_version
    response = client.post(
        f"/api/grading/batches/skill-2026/candidates/{candidate_id}/review", json=payload, headers=_auth(token)
    )
    return response.status_code


def _publish(client, token: str, candidate_id: int, *, reason: str = "对外发布", expected_version=None) -> int:
    payload = {"reason": reason}
    if expected_version is not None:
        payload["expected_version"] = expected_version
    response = client.post(
        f"/api/grading/batches/skill-2026/candidates/{candidate_id}/publish", json=payload, headers=_auth(token)
    )
    return response.status_code


def _revoke(client, token: str, *, reason: str = "发现评分错误") -> dict:
    response = client.post("/api/grading/batches/skill-2026/revoke", json={"reason": reason}, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_candidate_review_publish_supersede_and_rollback(client, admin):
    users = _setup_users(client, admin)
    _create_batch(client, _auth(users["director"]))

    c1 = _submit(client, users["scorer1"], "scorer-A", _scores(), {"average": 85.0, "pass_rate": 1.0})
    c2 = _submit(client, users["scorer2"], "scorer-B", _scores(85.0, 90.0, include_c=True), {"average": 81.67})

    # 未复核的候选不能直接发布
    assert _publish(client, users["director"], c2["id"]) == 409

    # 比较默认以最新候选 vs 当前发布；尚未发布时基线为空，全部学生表现为新增
    comparison = client.get("/api/grading/batches/skill-2026/comparison", headers=_auth(users["director"])).json()
    assert comparison["candidate"]["id"] == c2["id"]
    assert comparison["baseline"] is None
    assert comparison["structure"]["students_added"] == ["S1", "S2", "S3"]

    assert _review(client, users["director"], c1["id"], passed=True) == 200
    assert _publish(client, users["director"], c1["id"]) == 200

    # 查询同时呈现最近计算与当前发布
    view = client.get("/api/grading/batches/skill-2026", headers=_auth(users["director"])).json()
    assert view["latest_candidate_id"] == c2["id"]
    assert view["published_candidate_id"] == c1["id"]
    assert view["latest_candidate"]["status"] == "candidate"
    assert view["published_candidate"]["status"] == "published"

    # 指标差异（数值 delta）与结构变化（新增学生/指标）
    diff = client.get("/api/grading/batches/skill-2026/comparison", headers=_auth(users["director"])).json()
    assert diff["baseline"]["id"] == c1["id"] and diff["candidate"]["id"] == c2["id"]
    assert diff["structure"]["students_added"] == ["S3"]
    assert diff["student_changes"][0]["student_code"] == "S1"
    assert diff["student_changes"][0]["total_delta"] == 5.0
    assert diff["metrics_delta"]["average"]["delta"] == -3.33
    assert diff["metric_changes"]["pass_rate"] == {"from": 1.0, "to": None}

    # 显式基线参数
    explicit = client.get(
        f"/api/grading/batches/skill-2026/comparison?candidate_id={c2['id']}&baseline_id={c1['id']}",
        headers=_auth(users["director"]),
    ).json()
    assert explicit["identical"] is False

    # 发布 c2：c1 在同一事务内被顶替但记录保留
    assert _review(client, users["director"], c2["id"], passed=True) == 200
    assert _publish(client, users["director"], c2["id"], reason="第二套评分器复核无误") == 200
    view = client.get("/api/grading/batches/skill-2026", headers=_auth(users["director"])).json()
    assert view["published_candidate_id"] == c2["id"]
    statuses = {item["id"]: item["status"] for item in view["candidates"]}
    assert statuses == {c1["id"]: "superseded", c2["id"]: "published"}
    assert [p["kind"] for p in view["publications"]] == ["publish", "publish"]

    # 撤销 c2：只能回到上一份仍可用的 c1
    outcome = _revoke(client, users["director"])
    assert outcome["outcome"] == "rolled_back"
    assert outcome["revoked_candidate_id"] == c2["id"]
    assert outcome["published_candidate_id"] == c1["id"]
    view = client.get("/api/grading/batches/skill-2026", headers=_auth(users["director"])).json()
    assert view["published_candidate_id"] == c1["id"]
    statuses = {item["id"]: item["status"] for item in view["candidates"]}
    assert statuses[c1["id"]] == "published" and statuses[c2["id"]] == "revoked"
    assert view["publications"][-1]["kind"] == "rollback"

    # 再次撤销：c2 已撤销不可用，无回退目标，发布指针清空；旧记录不消失
    outcome = _revoke(client, users["director"], reason="c1 也有问题")
    assert outcome["outcome"] == "withdrawn"
    assert outcome["published_candidate_id"] is None
    view = client.get("/api/grading/batches/skill-2026", headers=_auth(users["director"])).json()
    assert view["published_candidate_id"] is None
    assert len(view["candidates"]) == 2
    assert {item["status"] for item in view["candidates"]} == {"revoked"}
    actions = [t["action"] for t in view["transitions"]]
    assert actions == ["submit", "submit", "review", "publish", "review", "supersede", "publish",
                       "revoke", "restore", "revoke"]

    # 无发布时撤销报错
    response = client.post("/api/grading/batches/skill-2026/revoke", json={"reason": "再次撤销"}, headers=_auth(users["director"]))
    assert response.status_code == 409


def test_separation_of_duties_and_permissions(client, admin):
    users = _setup_users(client, admin)
    _create_batch(client, _auth(users["director"]))

    # 评分员不能复核/发布；教务主任不能提交候选
    forbidden_submit = client.post(
        "/api/grading/batches/skill-2026/candidates",
        json={"scorer_code": "x", "scores": _scores()},
        headers=_auth(users["director"]),
    )
    assert forbidden_submit.status_code == 403

    c1 = _submit(client, users["scorer1"], "scorer-A", _scores(), {})
    forbidden_review = client.post(
        f"/api/grading/batches/skill-2026/candidates/{c1['id']}/review",
        json={"passed": True, "note": "self"},
        headers=_auth(users["scorer1"]),
    )
    assert forbidden_review.status_code == 403

    # 管理员拥有全部权限，但参与了计算就不能复核/发布自己的候选
    own = _submit(client, users["admin_token"], "scorer-admin", _scores(), {})
    own_review = client.post(
        f"/api/grading/batches/skill-2026/candidates/{own['id']}/review",
        json={"passed": True, "note": "self approve"},
        headers=_auth(users["admin_token"]),
    )
    assert own_review.status_code == 403

    # 主任复核通过管理员的候选后，管理员仍不能发布
    assert _review(client, users["director"], own["id"], passed=True) == 200
    assert _publish(client, users["admin_token"], own["id"]) == 403
    assert _publish(client, users["director"], own["id"]) == 200

    # 撤销同样受职责分离限制：当前发布由管理员计算，管理员不能撤销
    response = client.post(
        "/api/grading/batches/skill-2026/revoke", json={"reason": "wrong"}, headers=_auth(users["admin_token"])
    )
    assert response.status_code == 403


def test_late_approvals_cannot_override_later_decisions(client, admin):
    users = _setup_users(client, admin)
    _create_batch(client, _auth(users["director"]))
    c1 = _submit(client, users["scorer1"], "scorer-A", _scores(), {})

    # 主任驳回后，迟到的通过审批不能生效
    assert _review(client, users["director"], c1["id"], passed=False, note="数据异常") == 200
    assert _review(client, users["director"], c1["id"], passed=True, note="迟到的通过") == 409
    rejected = client.get("/api/grading/batches/skill-2026", headers=_auth(users["director"])).json()
    assert rejected["candidates"][0]["status"] == "rejected"

    c2 = _submit(client, users["scorer1"], "scorer-A", _scores(81.0), {})
    assert _review(client, users["director"], c2["id"], passed=True) == 200
    # 重复/迟到的复核不能改变已复核状态
    assert _review(client, users["director"], c2["id"], passed=False, note="迟到的驳回") == 409

    # 乐观版本：基于旧版本号的发布请求被拒绝
    assert _publish(client, users["director"], c2["id"], expected_version=1) == 409
    assert _publish(client, users["director"], c2["id"], expected_version=2) == 200

    # 已发布后迟到的复核不能覆盖
    assert _review(client, users["director"], c2["id"], passed=False, note="迟到的驳回") == 409
    view = client.get("/api/grading/batches/skill-2026", headers=_auth(users["director"])).json()
    assert view["published_candidate"]["status"] == "published"


def test_immutable_candidates_and_identical_comparison(client, admin):
    users = _setup_users(client, admin)
    _create_batch(client, _auth(users["director"]))
    c1 = _submit(client, users["scorer1"], "scorer-A", _scores(), {"average": 85.0})
    assert _review(client, users["director"], c1["id"], passed=True) == 200
    assert _publish(client, users["director"], c1["id"]) == 200

    # 第二套评分器产出完全相同的结果
    c2 = _submit(client, users["scorer2"], "scorer-B", _scores(), {"average": 85.0})
    diff = client.get(
        f"/api/grading/batches/skill-2026/comparison?candidate_id={c2['id']}", headers=_auth(users["director"])
    ).json()
    assert diff["identical"] is True
    assert diff["structure"] == {"students_added": [], "students_removed": [], "items_added": [], "items_removed": []}

    # 学生被移除、分项结构变化也能识别
    c3_payload_scores = [
        {"student_code": "S1", "student_name": "张三", "total": 80.0, "items": {"oral": 80.0}},
    ]
    c3 = _submit(client, users["scorer2"], "scorer-B", c3_payload_scores, {"average": 80.0})
    diff = client.get(
        f"/api/grading/batches/skill-2026/comparison?candidate_id={c3['id']}", headers=_auth(users["director"])
    ).json()
    assert diff["structure"]["students_removed"] == ["S2"]
    assert diff["structure"]["items_added"] == ["oral"]
    assert "practical" in diff["structure"]["items_removed"] and "written" in diff["structure"]["items_removed"]
    # S1 总分未变（total_delta=0），但分项结构发生变化，仍逐人呈现
    assert len(diff["student_changes"]) == 1
    assert diff["student_changes"][0]["student_code"] == "S1"
    assert diff["student_changes"][0]["total_delta"] == 0
    assert diff["student_changes"][0]["items_added"] == ["oral"]
