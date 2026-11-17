"""外部文献元数据入口：Crossref（DOI 核验）+ OpenAlex（论文、引用、相关文献）。

实现按职责拆开，这个文件只做汇总出口：

| 文件 | 职责 |
|---|---|
| `net.py` | 超时、有限重试、限速、磁盘缓存——所有外部请求的唯一通道 |
| `crossref.py` | DOI → 权威元数据（题目/作者/年份/期刊/引用数） |
| `openalex.py` | 工作记录、参考文献、施引论文、相关文献 |
| `normalize.py` | DOI、标题、作者的规范化（对比前的统一口径） |
| `verify.py` | 字段级一致性核查，并把每次查询写成 `SourceRecord` |

两条硬规则：

1. 取不到就如实失败（`BibliographyUnavailable` / `status=error` / `not_found`），
   **不编造、不返回空壳数据**。
2. 每条外部信息都能追到提供者、查询时间、外部 ID 与核实状态。

用法：

```python
from researchpilot.literature import bibliography as bib

client = bib.HttpClient()
work, response = bib.crossref.fetch_doi("10.1111/j.1365-246X.2007.03374.x", client)
report = bib.verify.verify_paper(paper, client)
```
"""

from __future__ import annotations

from . import crossref, net, normalize, openalex, verify
from .net import BibliographyUnavailable, CachedResponse, HttpClient
from .verify import FieldCheck, VerificationReport

__all__ = [
    "BibliographyUnavailable",
    "CachedResponse",
    "FieldCheck",
    "HttpClient",
    "VerificationReport",
    "crossref",
    "net",
    "normalize",
    "openalex",
    "verify",
]
