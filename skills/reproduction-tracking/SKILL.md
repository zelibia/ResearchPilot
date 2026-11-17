---
name: reproduction-tracking
description: 科研复现与实验跟踪规范。用于把论文方法拆解为可执行方案、定义验收指标、记录与比较真实运行、核查目标完成度和导出复现报告；严格区分执行成功、数值复现、证据完整与可外推，不用于论文原理讲解或全局项目问答。
---

# 复现与实验记录规范

复现的目标不是“跑通一次”，而是让另一个时刻的自己或另一位研究者能够回答：
用了什么输入、执行了什么、得到什么、与论文或目标差多少，以及哪些结论仍不能确认。

## 何时使用

- 把论文方法拆成可执行步骤；
- 设计复现目标、对照条件和验收指标；
- 记录一次真实实验运行；
- 比较方案版本、参数、数据或环境差异；
- 核查目标完成度与复现程度；
- 导出可审计的实验记录或复现报告。

需要解释论文原理时使用 `researchpilot-paper-reading`；
项目全局状态问答使用 `researchpilot-assistant`。

## 四个不能混同的判断

| 判断 | 回答的问题 | 不能替代 |
| --- | --- | --- |
| 运行状态 | 程序是否执行完成 | 数值是否复现 |
| 证据完整度 | 输入、环境、配置、日志、产物是否齐 | 方法是否正确 |
| 目标完成度 | 预设验收条件是否满足 | 全部论文结论是否成立 |
| 复现程度 | 在声明范围内与目标结果多接近 | 换数据、换场景后的可外推性 |

`succeeded` 只表示进程成功结束；它不等于“复现成功”，也不能直接写成
“论文结论已验证”。

## 核心铁律

1. **数值只来自真实程序输出。** 指标、参数、哈希、日志和图表不得估算、
   补齐或凭记忆重建。无法运行时明确写“未运行/无法运行”。
2. **每次运行都有唯一、不可复用的 `run_id`。** 重跑必须创建新记录，
   不得覆盖旧运行，也不得让模型自造运行编号。
3. **每次运行先形成最小证据包。** 输入标识、配置快照、代码与环境、实际命令、
   状态与时间、日志、输出文件及指标定义缺一项，就不能进入正式对照；
   记录仍应保留并明确标注缺失。
4. **配置快照与实际执行分开核对。** 参数类型、最终 argv、配置文件和程序实际
   执行的步骤都要保留。两者不一致时，以真实产物、标准输出和日志为准，
   并把差异写进异常或核查记录。
5. **指标必须自描述。** 至少写清名称、单位、定义式、分子分母、样本范围、
   时间窗、聚合方式、缺失值处理和所用参数。
6. **论文值与实测值分开。** 论文报告值、平台建议值、程序实测值不得混列；
   任何阈值和容差都要说明来源。
7. **假设与结论分离。** 用户猜想或 AI 建议标为“待验证”；实验后的结论必须
   关联 `run_id`、产物和指标，不能只凭一次成功运行升级为科研事实。
8. **全状态留痕。** `pending / running / succeeded / failed / skipped / cancelled`
   都是合法状态。失败、跳过和取消必须写原因；不能只保留成功记录。
9. **对照条件必须说清。** 两轮运行只允许一个意图变量不同，才算控制变量对照。
   如果数据、代码、参数、环境或硬件同时变化，只能写成差异比较，并列出所有差异。
10. **引擎与环境必须明确。** 写清后端、版本、解释器和关键依赖；未验证的引擎
    不得标为“已支持”，演示结果不得冒充正式复现。
11. **不泄露敏感信息。** 配置与日志入库前移除 API Key、密码、令牌和隐私数据；
    被脱敏的变量要保留名称与“已脱敏”状态，避免把不可执行快照说成可复现。
12. **历史结果必须标注来源。** 预先运行、导入或演示数据要标“历史运行”，
    给出原运行时间、数据范围、配置和代码版本，不得写成当前会话刚产生的证据。

## 最小证据包

每次进入正式比较的运行至少保留：

```text
运行身份：run_id / project_id / plan_version_id
输入：dataset_id / 文件清单 / SHA-256 / 数据清洗与筛选口径
配置：完整参数快照 / 随机种子 / 实际 argv / 工作目录
代码：engine / engine_version / 解释器 / 关键依赖 / 脚本哈希
环境：OS / 架构 / CPU/GPU / 容器镜像摘要 / 必要环境变量（已脱敏）
执行：开始与结束时间 / 退出码 / 状态 / 取消请求
日志：stdout、stderr、警告与原始错误
输出：相对路径 / 类型 / 大小 / SHA-256
指标：定义 / 单位 / 观测值 / 计算来源
```

缺少任何一项时：

- 该运行可以保存为“不完整记录”；
- 不得用于正式结论、目标完成判断或对外复现报告；
- `gate_failures` 要逐项写明缺失字段与影响。

## 从文献拆解复现方案

### 1. 绑定来源

方法步骤、论文参数、作者声明的前提和作者承认的局限，必须关联
`literature-evidence` 中的 evidence ID、页码或 ReaderAnchor。

论文没有说明的默认值不得替作者补全。应写：

```text
论文未说明
```

若为推进实验必须做选择，另立“自定义待验证假设”，不能写成“论文采用”。

### 2. 定义目标与验收

验收条件在运行前设定，至少包含：

```yaml
- criterion_id: C1
  statement: "要验证什么"
  metric: "指标名称"
  definition: "定义式、分子分母、时间窗和聚合方式"
  unit: ""
  paper_value: null          # 没有报告值就保持 null
  source_evidence_id: ""     # 论文值或自定义依据
  source_type: paper         # paper | custom
  tolerance: null            # 容差及来源；不能凭感觉补
```

如果论文没有报告可用于对照的数值，应把目标改成可验证的过程或一致性条件。
不得虚构“论文值”来制造成功率。

### 3. 拆成可执行步骤

```yaml
plan_basic:
  plan_version_id: ""
  plan_name: ""
  plan_version: 1
  status: draft              # draft | confirmed | discarded
  mode: reproduce            # reproduce | improve | own
  source_paper_id: ""
  source_doi: ""

objective: ""

method_steps:
  - step_id: S1
    name: ""
    input:
      data_ids: []
      parameters: {}
    operation: ""
    output:
      artifacts: []
      expected_checks: []
    source_evidence_ids: []
    implementation_status: missing   # missing | planned | implemented | tested
    progress_status: todo            # todo | doing | done | skipped
    engine_stage: ""                 # qc | preprocess | xcorr | stack | 空
    run_id: ""                       # 仅指向真实 ExperimentRun

assumptions:
  - statement: ""
    type: paper                # paper | custom_to_verify
    source_evidence_ids: []

risks:
  - statement: ""
    level: medium              # low | medium | high
    evidence: ""
```

步骤按实际执行顺序排列。输入、处理、输出和完成检查都要写；只有一句
“进行预处理”不足以构成可执行方案。

`progress_status` 是人工进度，不是执行结果；某一步是否真正成功只看关联
`run_id` 对应 `ExperimentRun.status`。已确认的方案版本按新版本演进，
不要把已确认版本原地改成“废弃”后仍作为同一版本对外引用。

## 运行记录

使用 ResearchPilot 现有字段时，以下内容分别落到对应对象：

| 内容 | 主要字段/对象 |
| --- | --- |
| 方案与步骤 | `PlanVersion`、计划步骤及文献证据 |
| 运行身份与命令 | `ExperimentRun.id/project_id/plan_version_id/command` |
| 环境 | `engine/engine_version/interpreter/dependencies` |
| 代码版本 | `script_hashes` |
| 数据输入 | `dataset_id/data_hash/inputs/stations/channels/time_window` |
| 时间与结果状态 | `status/started_at/ended_at/exit_code/duration_s` |
| 日志与错误 | `log_path/errors/warnings` |
| 产物 | `Artifact(type,path,sha256)` 以及运行 `summary` |
| 人工观察 | `LabNote` |
| 假设状态 | `Hypothesis.status/evidence_run_ids` |
| 目标评价 | `GoalAssessment` |

推荐记录形状：

```yaml
run:
  run_id: ""                 # 服务端生成的唯一 ID
  project_id: ""
  plan_version_id: ""
  engine: ""
  engine_version: ""
  interpreter: ""
  dependencies: {}
  script_hashes: {}

  dataset_id: ""
  data_hash: ""
  inputs: []                 # 相对路径；每项最好另有 SHA-256
  stations: []
  channels: []
  time_window: ""

  command: ""                # 实际执行的命令/argv
  work_dir: ""
  config_path: ""
  config_snapshot: {}
  random_seeds: {}

  status: pending
  started_at: ""
  ended_at: ""
  duration_s: null
  exit_code: null
  cancel_requested: false
  errors: []
  warnings: []
  log_path: ""

  outputs:
    - type: data             # figure | data | report | log | other
      path: ""               # 相对运行目录
      sha256: ""
      size_bytes: null

  metrics:
    - name: ""
      value: null            # 只允许真实观测值
      unit: ""
      definition: ""
      artifact_path: ""

  notes: []
```

不要把 `pid/pgid` 当作科研证据；它们只服务于任务取消和进程管理。
`work_dir` 若包含个人信息，可在展示层脱敏，但内部仍需保留可追溯的映射。

## 指标与复现程度

### 指标定义

一个可比较指标至少写清：

```text
名称 / 单位 / 定义式 / 分子 / 分母 / 样本范围 / 时间窗
滤波与预处理 / 聚合方式 / 缺失值处理 / 随机种子 / 计算脚本 / run_id
```

同名指标若定义不同，不能直接比较。例如两个“SNR”必须确认频段、窗口、
噪声估计方法和事件筛选是否一致。

### 复现程度

用覆盖范围而非单一“成功率”表达：

```text
not_assessable   没有目标值或无法比较
process_only     只证明流程执行成功，尚未完成目标对照
partial          仅部分步骤或指标完成对照
equivalent       在声明容差和条件下达到目标
exact            结果在论文报告精度内一致；仅有哈希相同时才可写字节级一致
not_reproduced   执行完成但目标值未达到
```

如确需汇总“复现成功率”，必须同时给出：

```text
成功率 = 达到预设复现标准的条目数 / 可评估条目数
```

并列出不可评估条目、容差、目标来源和分母。没有可评估条目时写
“无法计算”，不能把 `0/0` 写成 0%。

## 对照与版本比较

比较前先核对：

1. 两次运行是否属于同一研究问题与方案版本谱系；
2. 输入数据、预处理、代码、配置、环境和硬件是否可追溯；
3. 指标定义和时间窗是否一致；
4. 是否只有一个人为改变的意图变量。

固定比较结构：

| 对比项 | 运行 A | 运行 B | 差异类型 | 可比较性/影响 |
| --- | --- | --- | --- | --- |
| 输入 | 数据版本 + 哈希 | 数据版本 + 哈希 | data | 是否有可比样本 |
| 代码 | commit/脚本哈希 | commit/脚本哈希 | code | 是否改变算法 |
| 参数 | 参数快照 | 参数快照 | config | 哪个参数是意图变量 |
| 环境 | 引擎/依赖/硬件 | 引擎/依赖/硬件 | environment | 是否影响数值 |
| 指标 | 值 + 定义 | 值 + 定义 | measurement | 是否同口径 |
| 产物 | 文件 + SHA-256 | 文件 + SHA-256 | output | 是否字节一致或仅近似 |

差异类型使用：

```text
data | code | config | environment | hardware | random | measurement
```

如果存在多个意图变量，称“并发差异比较”，不要称“控制变量实验”。
随机算法只跑一次时，不得把微小差异直接归因于参数；需要重复运行或明确
“随机性未评估”。

## 目标完成度与结论

逐条评价运行前的验收条件：

```yaml
- criterion_id: C1
  assessment: done          # done | partial | not_done | failed
  observed_value: null
  unit: ""
  run_ids: []
  evidence_ids: []
  reason: ""
  next_step: ""
```

判定规则：

- `done`：有真实运行和输出证据，且满足运行前定义的全部验收条件；
- `partial`：只有部分指标或样本范围达到条件；
- `not_done`：尚未执行或证据不足；
- `failed`：已执行，但结果未达到验收条件或运行失败。

“运行成功”但目标未达到时，目标状态应是 `failed` 或 `partial`，
不能写 `done`。没有任何实验证据的目标不得标为 `completed/done`。

复现结论最多写到证据支持的范围。例如：

```text
在当前数据、代码和环境快照下，指标 C1 与论文报告值相差 0.4%，
落入预设的 ±1% 容差；C2 未执行，因此本结果不构成论文全部结论的复现。
```

不得由一个指标通过就写“论文完全复现”或“方法普遍有效”。

## 复现报告结构

导出报告按以下顺序组织：

1. 项目与运行标识；
2. 来源论文、DOI 与证据范围；
3. 研究目标与复现模式；
4. 方案版本、步骤、假设和缺失信息；
5. 数据、代码、配置、环境与命令快照；
6. 运行状态、失败/跳过/取消记录；
7. 论文值、实测值、容差与偏差；
8. 版本比较及差异归因；
9. 目标完成度；
10. 复现程度、覆盖范围与不可外推边界；
11. 后续最小验证动作。

报告中的每个数值都要能追到 `run_id + artifact/metric`；
每个方法前提都要能追到文献 evidence ID 或明确标为自定义假设。

## 与相邻技能的边界

- `literature-evidence` 提供方法、参数、假设和验收指标的原文献证据；
  本技能不把方法描述自动升级为已复现事实。
- `researchpilot-paper-reading` 负责论文原理、公式和全文深读；
  本技能只处理工程化步骤、运行记录与结果对照。
- `researchpilot-assistant` 负责项目状态问答和操作引导；
  本技能不得替它声称“已经运行”“已经保存”或“已经复现”。

## 提交前检查

- [ ] `run_id` 唯一且由服务端生成，重跑没有覆盖旧记录？
- [ ] 输入文件、数据版本和 SHA-256 可追溯？
- [ ] 配置快照、最终 argv、随机种子和实际执行步骤一致？
- [ ] 引擎、解释器、依赖、脚本哈希和环境信息齐全？
- [ ] 状态、时间、退出码、日志、失败与取消原因已保存？
- [ ] 每个输出文件都有相对路径、类型和 SHA-256？
- [ ] 每个指标都有定义、单位、范围和计算来源？
- [ ] 论文值、建议值和实测值没有混写？
- [ ] 运行成功、目标完成和数值复现没有相互冒充？
- [ ] 对照是否只有一个意图变量变化，其他差异是否列全？
- [ ] 结论是否限定在当前数据、环境与证据覆盖范围内？
- [ ] 历史、演示和预先运行结果是否明确标注？
