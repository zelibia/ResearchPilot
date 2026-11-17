# storage —— 唯一可信的科研记录

承载"研究目标 ↔ 计划版本 ↔ 实验运行 ↔ 结果"的可追溯关系，是 Outcomes 能逐条对照的基础。

| 文件 | 职责 |
|---|---|
| `models.py` | Pydantic 模型，对应 PRD 第 7 节的 11 个数据对象 |
| `database.py` | SQLite 读写 |
| `v116/` | v1.16 全局文献、批注、大纲、总结的 SQLAlchemy 模型、服务与迁移 |
| `../screening/` | P0-02 初筛候选、AI 评估、用户决策与正式入库事务 |

v1.16 数据契约见 `docs/00_项目启动/P0-01_contracts_v1.16.ts` 与
`docs/00_项目启动/P0-01_schema_v1.16.sql`。

唯一可信源就是这个 SQLite 库加每个 run 的目录。
女娲的知识库和数据表只是页面查询副本，不是唯一副本——官方 `.agent` 导出不含数据表与知识库的实际数据。

硬约束：本目录不得 import 女娲 SDK 或私有 API。
