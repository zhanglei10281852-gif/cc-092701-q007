# 职业教学任务运营服务

这是一个面向职业院校教务团队、授课教师和课程管理员的 Python 后端服务，用于管理课程任务模板、学员提交、执行队列、教师工作者、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 技能考试成绩治理

同一批考试被两套评分器分别计算后，成绩以不可变候选的形式进入治理流程，接口前缀为 `/api/grading`：

```text
candidate（待复核）──review──▶ reviewed ──publish──▶ published
        │                                              │
      reject                                  supersede（被新发布顶替，仍可回退）
        │                                              │
        ▼                                          revoke（撤销作废，终态）
     rejected                              撤销时自动回到发布链上最近一份仍可用的结果
```

- `POST /api/grading/batches/{code}/candidates` 提交评分器结果（需 `grades.compute`），每次提交生成带摘要的不可变候选并更新“最近计算”指针。
- `POST .../candidates/{id}/review` 复核通过或驳回（需 `grades.review`），支持 `expected_version` 乐观版本；驳回或发布后的迟到审批一律返回 409。
- `POST .../candidates/{id}/publish` 发布（需 `grades.publish`），在单个即时事务内切换当前发布指针、顶替旧发布并写入发布链。
- `POST /api/grading/batches/{code}/revoke` 撤销当前发布，只能回到发布链上最近一份未撤销的结果；没有可回退结果时仅撤下指针，旧记录永不删除。
- `GET .../comparison` 比较候选与基线（默认最近候选 vs 当前发布），返回指标数值差异、指标增删、学生增删、分项增删和逐人总分 delta。
- `GET /api/grading/batches/{code}` 同时呈现最近计算候选与当前发布版本，以及完整候选列表、发布链和状态转换台账。

发布、复核和撤销均要求操作者拥有相应权限，且不能是该候选的计算人（`computed_by_id` 职责分离校验）。种子角色 `scorer` 仅可提交候选，`grades_director` 可复核、发布与撤销。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/grading/       成绩候选、复核、发布、撤销的状态机与差异比较
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
