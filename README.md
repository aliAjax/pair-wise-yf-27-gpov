# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`（甲馆/A 侧审查员）、`reviewer2`（乙馆/B 侧审查员）、`claimant1`、`public`。

## 两馆对账台账

两馆各自导入清单，系统按**稳定编号**配对，持有人、流转时间、本方编号对不上的行记差异，供双方审查员逐条确认。

- `POST /api/recon/sync`（staff）：按侧（`A`/`B`）导入清单，行字段 `stable_no`、`client_row_id`、`local_ref`、`holder`、`transfer_date`。按 `(侧, 行键, 内容哈希)` 去重，**失败重试只补未同步行**，已同步且内容未变的行直接跳过，不重建台账版本。
- `GET /api/recon/pairs`、`GET /api/recon/pairs/{stable_no}`：当前台账与版本史（staff/reviewer）。状态为 `matched` / `discrepancy`（附逐字段差异）/ `one_sided`（仅一侧有记录，不能确认）。
- `POST /api/recon/pairs/{stable_no}/confirm`（审查员）：审查员由所属侧（`reviewer1`=A、`reviewer2`=B）确认当前版本。同一版本每侧唯一确认，唯一约束加写锁保证**先确认者赢，并发提交的另一方得到 `409 confirmation_conflict`**。
- `GET /api/recon/pairs/{stable_no}/handoff`（staff/reviewer）：交接结论只认**当前版本且双方审查员均已确认**；单侧确认或无确认返回 `409 recon_not_ready`。
- 任一侧记录变化（同 `client_row_id` 新修订或稳定编号迁移）时自动重建受影响编号：未双方确认的版本整版标记 `superseded_voided`，其确认留痕但失效；已双方确认的版本标记 `frozen` 冻结存档，另开新版本重走双确认，**交接和返还不得复用旧结论**。
- `POST /api/objects/{id}/claims` 可带 `stable_no` 关联台账；主张在 `negotiating → resolved_return` 时校验当前版本已双确认，否则返回 `recon_not_ready`，即**只有双方审查员都确认，权利主张才能推进返还**。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
