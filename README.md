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

## 成绩结果的候选、复核、发布与撤销

同一批技能考试可由两套评分器分别计算（`POST /api/compute/tasks/{id}/rescore` 追加结果版本）。每份对外候选经历可审计的状态转换：

```text
candidate ──start_review──▶ in_review ──approve──▶ approved ──publish──▶ published
    │                             │                    │                    │
    └────────reject───────────────┴────────────────────┘                    │
                                                                            │ 被更新发布取代
                                                                       superseded
                                       published ──revoke──▶ revoked，并回退到上一份 superseded（仍可用）结果
```

- `POST /api/compute/tasks/{task_id}/releases`：把某个结果版本登记为候选（需 Bearer 登录）。
- `POST /api/compute/releases/{id}/review-start|approve|reject`：需要 `compute.review` 权限；批准/驳回必须填写意见。
- `POST /api/compute/releases/{id}/publish`：需要 `compute.publish` 权限，且确认人不得是该结果的计算者或候选提交人（计算回避）；状态切换与当前发布指针在同一事务完成，旧发布转为 `superseded` 但记录保留。
- `POST /api/compute/releases/{id}/revoke`：需要 `compute.revoke` 权限；只能撤销当前 `published` 结果，并自动回到上一份仍可用（`superseded`）的结果；没有可回退版本时清空当前指针。迟到的批准/发布会因状态不匹配返回 409，不能覆盖后续决定。
- `GET /api/compute/tasks/{id}/releases/compare?base=&target=`：返回两份候选间的指标差异（新增/移除/变化及数值差量）和结果结构变化（路径新增、移除、类型或值变化）。
- `GET /api/compute/tasks/{id}/releases` 与任务详情同时呈现 `latest_result`（最近计算版本）和 `current_published`（当前发布版本），并附按顺序排列的全部转换事件。

所有转换写入 `compute_result_release_events`（操作人、动作、意见、前后状态快照），候选记录本身永不物理删除。

