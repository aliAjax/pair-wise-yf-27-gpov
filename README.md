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

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

## 对账台账（双方清单对账）

两家博物馆各自维护藏品清单，按**稳定编号**配对成对账台账：对不上的记为待核（`pending`），编号不一致的记为差异（`discrepancy`）交审查员确认，双方审查员都确认后结论才生效（`confirmed`），交接只带已确认结论。

- `POST /api/recon/batches`：建立对账批次。
- `POST /api/recon/batches/{id}/sync`：同步某一方（`side` 为 `A`/`B`）的清单。已核完的行不碰，重试只补未核完行；任一侧记录变化会作废未确认结果。
- `GET /api/recon/matches`、`GET /api/recon/matches/{id}`：查看对账结果（可按 `?status=` 过滤）。
- `POST /api/recon/matches/{id}/confirm`：审查员确认。需携带 `version` 做乐观锁，两人同时提交只算先确认的，另一个返回 `409 conflict`，刷新后重试即可。
- `POST /api/recon/matches/{id}/discrepancy`、`/reject`：标记差异或拒绝。
- `GET /api/recon/handoff`：交接，只输出双方都确认的结论。

审查员分属 A/B 两方（`reviewer_a`、`reviewer_b`），确认时取其归属方。只有双方审查员都确认，对应藏品的权利主张才能推进返还（`resolved_return`），否则返回 `recon_not_confirmed`。
