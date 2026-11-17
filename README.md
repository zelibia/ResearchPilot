# ResearchPilot
科研文献管理与方法复现全流程平台，覆盖文献精读、引用关系图谱、复现规划、实验跟踪、成果产出完整链路，面向地震学等理工科研场景设计。

## 核心功能
### 四大工作台
1.  **文献探索**
    - PDF 沉浸式精读 + 结构化 AI 解读，支持精简摘要/方法重点/标准详细三种生成模式
    - 引用关系自动拉取与人工裁决，可视化引用图谱，支持版本管理与下游导出
    - 文献库批量管理，元数据自动核验，支持标签分类与检索

2.  **复现规划**
    - 论文方法拆解为可执行分步方案，版本化管理，支持草稿与已确认版本
    - 验收指标定义、风险点标注、假设与结论分离
    - 多方案横向对比，一键导入实验工作台

3.  **实验工作台**
    - 实验运行全要素记录：配置快照、数据标识、引擎版本、运行日志、输出文件
    - 运行状态跟踪（成功/失败/跳过），异常与问题记录
    - 多轮实验结果对比，变量控制校验

4.  **科研成果**
    - 结构化报告自动生成，章节化管理，证据关联原文
    - 方法演进大纲、文献对比综述自动导出
    - Markdown 报告导出，支持版本追溯

### 全局能力
- **悬浮 AI 助手**：支持右侧停靠/悬浮窗口双模式，对话历史按项目隔离，自动感知当前页面上下文
- **引用关系图谱**：放射/时间轴双布局，节点可跳转，支持人工微调、版本管理、批量操作
- **证据规范体系**：四层证据分级，所有结论绑定原文页码，原文与推断严格分离
- **数据安全**：本地 SQLite 存储，所有数据本地可控，支持备份与迁移

## 目录结构
```
ResearchPilot/
├── app.py                  # 项目主入口
├── requirements.txt         # Python 依赖清单
├── run_windows.bat          # Windows 一键启动脚本
├── .env.example             # 环境变量模板
├── configs/                # 全局配置
├── src/                    # 核心业务源码
│   └── researchpilot/
│       ├── ui/              # 四个工作台与全局组件界面
│       ├── storage/         # 数据模型与存储层
│       ├── literature/      # 文献处理与引用逻辑
│       ├── agent/           # AI 调用与解析引擎
│       └── exports/         # 报告与导出模块
├── packages/               # 独立组件
│   └── researchpilot_pdf_reader/  # PDF 阅读器组件
├── skills/                 # AI 技能规范库
│   ├── literature-evidence/
│   ├── reproduction-tracking/
│   ├── researchpilot-paper-reading/
│   └── researchpilot-assistant/
├── scripts/                # 工具脚本与迁移脚本
├── tests/                  # 自动化测试套件
├── docs/                   # 设计文档与验收报告
├── demo_data/              # 示例数据
├── outputs/                # 运行数据与输出（运行时生成）
└── logs/                   # 运行日志（运行时生成）
```

## 快速开始
### 环境要求
- Python 3.10+
- Windows / WSL2 / Linux
- 可选：DeepSeek / 其他兼容 OpenAI 协议的大模型 API

### 安装与启动
1.  安装依赖
    ```bash
    pip install -r requirements.txt
    ```

2.  配置环境变量
    复制 `.env.example` 为 `.env`，按需配置：
    - `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`：大模型调用配置
    - `RESEARCHPILOT_DB_PATH`：数据库文件路径
    - `RESEARCHPILOT_PORT`：服务端口

3.  启动应用
    - Windows：双击 `run_windows.bat`
    - 命令行：
      ```bash
      streamlit run app.py
      ```

4.  访问
    浏览器打开 `[http://localhost:8501](http://localhost:8501)` 即可使用

## 版本说明
### v1.16
- P0 全链路打通：文献上传→初筛评估→精读标注→大纲管理→阶段总结完整业务闭环
- 全局 UI 极简改版：对齐 Readwise 阅读风格，四工作台统一布局，去除冗余文案与控件
- 引用关系图谱上线：自动生成+人工微调，支持版本管理、批量操作、下游导出
- 全局 AI 助手升级：右侧停靠/悬浮双模式，对话历史管理，上下文自动感知
- 四层证据规范落地：原文依据/AI解释/背景补充/待核实严格分离，所有结论可溯源
- 全量自动化测试覆盖，核心功能回归验证通过

## 许可证
本项目仅供学习与科研使用。
第三方依赖许可证见 `LICENSES.md`。
