# ui —— 文献阅读工作区

当前对外的唯一主页面是 `literature_page.py`，由 `app.py` 直接调用。

| 模块 | 作用 |
|---|---|
| `literature_page.py` | 项目文献库、元数据、引用关系与详情 |
| `pdf_workspace.py` | 原文与 AI 解读双栏阅读 |
| `reading_guide.py` | 精简摘要、方法重点、标准详细三模式与证据定位 |
| `screening_page.py` | 候选文献收件箱 |
| `assistant.py` | 当前项目文献阅读助手 |

`planner_page.py`、`experiment_page.py` 和 `outcomes_page.py` 暂时保留在仓库中供回滚，
但不再由应用导入或路由。后续迁移通过稳定后再物理删除。

规则：

1. **本层不放业务逻辑。** 解析、检索、证据核验和持久化都放在
   `literature/`、`agent/`、`screening/` 与 `storage/` 里，这里只做调用与展示。
2. **持久化一律走 `storage/repository.py`**，页面里不写 SQL。
3. **未实现的功能要如实写出来，不放假按钮。** 点不到结果的按钮比没有按钮更糟。
4. 项目库、阅读内容和助手上文共用同一个 `project_id`。
