"""全文索引：把一篇论文按**章节 / 语义片段**组织起来（P0-B）。

为什么需要它：AI 抽取以前只把前 8 页、1.2 万字符送进模型，22 页的论文等于只读了
前三分之一，而界面上却写着"AI 文献分析"。这里把"读了多少"变成可核查的事实：

- **分页正文**来自入库时生成的 `<pdf>.pages.txt`（只读，不改原文件）；
- **章节**优先用 PyMuPDF 的 ``get_toc()`` 书签；没有书签时才用标题样式做**候选**
  大纲，并明确标注为候选（不凭空生成章节号）；
- **段落**带页码与页内序号，坐标按需从 PDF 文本层取（取不到就明确说取不到）；
- **检索**按问题跨全文召回段落，供跨章节证据召回使用。

三条纪律：

1. 只读：从不写入、不覆盖原始 PDF；坐标只读文本层。
2. 不编造：没有目录书签就只给候选大纲；没有文本层就不给坐标，绝不猜。
3. 不越界：同一篇论文的索引只属于这篇论文，不含其它文献内容。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..storage.models import Paper
from . import paths as paper_paths
from . import pdf_view

try:  # PyMuPDF 1.24+ 推荐 import pymupdf；旧名字 fitz 仍可用
    import pymupdf as _fitz
except Exception:  # pragma: no cover - 兼容旧环境
    import fitz as _fitz  # type: ignore

# 段落切分：连续空行分段；单页没有空行时整页算一段。
_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
# 页眉页脚：形如 "1239"、"Processing ambient noise ..."（全篇重复的行）
_RUNNING_LINE_RE = re.compile(r"^(?:\d{1,4}|[A-Z][A-Za-z ,\-']{6,70})$")

# 标题样式：编号标题（"1. Introduction" / "2.1 Data"）、全大写标题、常见章节名
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][^\n]{2,80})$")
_PLAIN_HEADING_RE = re.compile(r"^\s*(Abstract|Introduction|Conclusions?|Discussion|"
                               r"References|Acknowledg\w*|Data and methods?|Methods?)\s*$",
                               re.IGNORECASE)
# 图注里的子图标签（a) (b) c)）不是标题
_FIGURE_LABEL_RE = re.compile(r"^\(?[a-z0-9]\)?$")
# 关键词行（"Key words: ..."）不是章节标题
_KEYWORDS_RE = re.compile(r"^(key\s*words?|keywords)\s*:", re.IGNORECASE)

# 检索用：英文词（≥3 字母）与中文二元组
_WORD_RE = re.compile(r"[a-z][a-z0-9_\-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

DEFAULT_SECTION_CHARS = 14000
DEFAULT_MAX_REQUESTS = 10


@dataclass(frozen=True)
class Section:
    """一段有页码范围的章节。

    ``source`` 说明这段标题是怎么来的，界面必须如实展示：
    ``toc`` =  PDF 自带书签（可信）；``heading`` = 按原文排版（字号/加粗）读出的标题；
    ``pages`` = 完全没有结构时的页组。
    """

    index: int
    title: str
    start_page: int
    end_page: int
    source: str = "pages"
    level: int = 1

    @property
    def is_confirmed_toc(self) -> bool:
        return self.source == "toc"


@dataclass(frozen=True)
class Heading:
    """从 PDF 排版里读出的一行标题（不是猜的：字号/加粗来自真实文本层）。"""

    title: str
    page: int
    level: int
    size: float = 0.0


@dataclass(frozen=True)
class Passage:
    """一段原文。``coords`` 为空表示这一页没有可用文本层，不是"没有坐标"。"""

    paper_id: str
    page: int
    order: int                 # 页内段序号，1 起
    section_index: int         # 所属章节序号，0 = 不属于任何章节
    text: str
    coords: tuple[tuple[float, float, float, float], ...] = ()
    # 以整页重复的页眉/图注标签开头（"G. D. Bensen et al." / "time (sec) a) b)…"）。
    # 检索时降权，但**正文一个字都不删**——段落必须与原文逐字一致，否则定位会失准。
    noise_lead: bool = False

    @property
    def locator(self) -> str:
        return f"第 {self.page} 页 · 第 {self.order} 段"


@dataclass
class PaperIndex:
    paper_id: str
    title: str
    page_count: int
    pages: list[str] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    toc_available: bool = False
    coord_pages: set[int] = field(default_factory=set)   # 有文本层、能算坐标的页
    notes: list[str] = field(default_factory=list)

    @property
    def section_source(self) -> str:
        """大纲的可靠级别：toc（书签） > heading（候选） > pages（无结构）。"""
        if self.toc_available:
            return "toc"
        if self.sections and self.sections[0].source == "heading":
            return "heading"
        return "pages"

    def section_of_page(self, page: int) -> Section | None:
        """当前页属于哪一节：取**最后一个**起始页不晚于它的标题。

        一章经常和上一章共用同一页（标题在页面中途），所以"包含关系"会有重叠，
        按阅读顺序取最近的一个标题才符合直觉。
        """
        found: Section | None = None
        for section in self.sections:
            if section.start_page <= page:
                found = section
            elif found is not None:
                break
        return found

    def section_passages(self, index: int) -> list[Passage]:
        return [item for item in self.passages if item.section_index == index]


# ---------------------------------------------------------------- 读取与组装


def read_toc(pdf_path: Path) -> list[tuple[int, str, int]]:
    """读 PDF 书签：[(层级, 标题, 起始页 1 起)]。没有书签/读不到就返回空。"""
    try:
        with _fitz.open(pdf_path) as doc:          # 只读打开
            toc = doc.get_toc(simple=True) or []
    except Exception:  # noqa: BLE001 - 读不到书签不是错误，退回候选大纲
        return []
    result: list[tuple[int, str, int]] = []
    for entry in toc:
        if len(entry) < 3:
            continue
        level, title, page = entry[0], str(entry[1] or "").strip(), int(entry[2] or 0)
        if title and page > 0:
            result.append((int(level), title, page))
    return result


def _split_paragraphs(page_text: str) -> list[str]:
    return [text for text, _offset in _split_paragraphs_with_offsets(page_text)]


def _split_paragraphs_with_offsets(page_text: str) -> list[tuple[str, int]]:
    """切段并保留**段落在这页正文里的字符偏移**（用它把段落归到章节）。

    按页正文里的空行切段；一整页没有空行时（排版本如此）再按句号切成
    长度适中的片段，否则一页只有一个"段落"，检索和章节归属都会很粗。

    **一个字都不删、不加**：行与行之间保留换行，段落就是这一页正文的一段连续切片。
    页眉/图注标签只做标记（``noise_lead``），不在这里剔除——剔了段落就不再是
    原文的逐字切片，按段落定位原文时会整段落空（实测踩过这个坑）。
    """
    raw = page_text or ""
    blocks: list[tuple[str, int]] = []
    starts = [0]
    for match in _PARA_SPLIT_RE.finditer(raw):
        starts.append(match.end())
    starts.append(len(raw))
    for position in range(len(starts) - 1):
        segment = raw[starts[position]:starts[position + 1]]
        offset = starts[position]
        text = segment.strip()
        if text:
            blocks.append((text, offset))

    # 一页只有一段时按句子再切（只在需要时切，段落边界仍以原文空行为准）
    out: list[tuple[str, int]] = []
    for text, offset in blocks:
        if len(text) <= _MAX_PASSAGE_CHARS:
            out.append((text, offset))
            continue
        cursor = offset
        buffer = ""
        buffer_start = offset
        for piece in _SENTENCE_RE.split(f" {text} "):
            if not piece.strip():
                continue
            if not buffer:
                buffer_start = cursor
            buffer = f"{buffer} {piece}".strip() if buffer else piece.strip()
            cursor += len(piece)
            if len(buffer) >= _TARGET_PASSAGE_CHARS:
                out.append((buffer, buffer_start))
                buffer = ""
        if buffer:
            out.append((buffer, buffer_start))
    return out


_MAX_PASSAGE_CHARS = 1400
_TARGET_PASSAGE_CHARS = 700
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")


def _normalize_span(text: str) -> str:
    """把排版用的小型大写字母间距还原成正常单词。

    PDF 里的小节标题常排成 ``S TAT I O N``（字与字之间插空格）。PyMuPDF 的
    span 已经按"词"切开，所以这里只需判断这个 span 是不是"字母间距写法"，
    是就把内部空格去掉——**只改排版，不改字**。
    """
    raw = str(text or "")
    parts = raw.split()
    tokens = [token for token in parts if token.strip("-")]
    if len(tokens) >= 2:
        single = sum(1 for token in tokens if len(token.strip("-")) == 1)
        letterspaced = single / len(tokens) >= 0.6
        # 全大写且夹着单字母碎片（如 "SU MMARY"）也是字母间距写法
        if not letterspaced and raw.isupper() and any(
                len(token.strip("-")) < 3 for token in tokens):
            letterspaced = True
        if letterspaced:
            return "".join(parts)
    return raw


def _spans_to_words(spans: list[dict]) -> list[str]:
    """一行文本 → 词表（顺带还原字母间距写法）。"""
    words: list[str] = []
    for span in spans:
        text = str(span.get("text") or "")
        if not text.strip():
            continue
        words.append(_normalize_span(text))
    return words


def read_headings(pdf_path: Path) -> list[Heading]:
    """从 PDF 文本层的**排版**读出章节标题（字号 + 加粗），不生成、不猜章节号。

    判据：加粗、字号等于正文字号（小型大写）或略大、长度合理、不是图注/页码。
    同一节标题占两行时（如 "3 CROSS-CORRELATION, STACKING" + "AND SIGNAL
    EMERGENCE"）按"页内相邻两行"合并。
    """
    try:
        doc = _fitz.open(pdf_path)
    except Exception:  # noqa: BLE001 - 打不开就交给调用方降级
        return []

    try:
        rows: list[tuple[int, int, str, float, bool]] = []   # 页, 页内序, 文本, 字号, 加粗
        weighted: dict[float, int] = {}
        for page_no in range(doc.page_count):
            page = doc[page_no]
            order = 0
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    spans = line.get("spans") or []
                    words = _spans_to_words(spans)
                    if not words:
                        continue
                    text = " ".join(words).strip()
                    sizes = [float(span.get("size") or 0) for span in spans
                             if str(span.get("text") or "").strip()]
                    size = round(max(sizes), 1) if sizes else 0.0
                    bold = any(int(span.get("flags") or 0) & 16 for span in spans)
                    order += 1
                    rows.append((page_no + 1, order, text, size, bold))
                    weighted[size] = weighted.get(size, 0) + len(text)
        if not rows:
            return []
        body = max(weighted.items(), key=lambda item: item[1])[0]
        candidates = [
            row for row in rows
            if row[4]                                     # 加粗
            and body > 0 and body * 0.95 <= row[3] <= body * 1.30
            and 2 <= len(row[2]) <= 95
            and sum(char.isalpha() for char in row[2]) >= 3
            and not _FIGURE_LABEL_RE.match(row[2])
        ]
        if not candidates:
            return []

        merged: list[Heading] = []
        last_order = -10
        for page_no, order, text, size, _bold in candidates:
            # 同一页、紧挨着的两行且第二行不是新编号 → 同一个标题的续行
            if (merged and merged[-1].page == page_no and last_order + 1 == order
                    and not _NUMBERED_HEADING_RE.match(text)):
                previous = merged.pop()
                text = f"{previous.title} {text}"
                page_no, size = previous.page, previous.size
                order = last_order
            match = _NUMBERED_HEADING_RE.match(text)
            level = 2 if (match and "." in match.group(1)) else 1
            merged.append(Heading(title=text, page=page_no, level=level, size=size))
            last_order = order
        # 合并之后再过滤：关键词行、纯列表行不是章节标题
        return [
            item for item in merged
            if not item.title.rstrip().endswith((",", ";", ":"))
            and not _KEYWORDS_RE.match(item.title)
        ]
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass


def _sections_from_headings(headings: list[Heading], total_pages: int) -> list[Section]:
    """标题 → 连续覆盖全篇的章节段。标题之间的页都归上一节。"""
    unique: list[Heading] = []
    for item in headings:
        if unique and unique[-1].title == item.title and unique[-1].page == item.page:
            continue
        unique.append(item)
    if not unique:
        return []
    sections: list[Section] = []
    for position, item in enumerate(unique):
        next_page = unique[position + 1].page if position + 1 < len(unique) else total_pages
        end = max(item.page, min(next_page - 1, total_pages)) if position + 1 < len(unique) else total_pages
        sections.append(Section(index=len(sections) + 1, title=item.title,
                                start_page=item.page, end_page=end,
                                source="heading", level=item.level))
    return sections


def _loose_key(text: str) -> str:
    """宽松 key：去空白、统一小写与常见连字符，用来在按页正文里找标题位置。"""
    value = str(text or "").lower()
    for char in " \t\r\n\u00a0‐‑‒–—―-":
        value = value.replace(char, "")
    return value


def _heading_offsets(index: PaperIndex, page_text: str,
                     page_no: int) -> list[tuple[int, int]]:
    """这一页上每个章节标题在正文里的字符位置（找不到就不列）。"""
    key = _loose_key(page_text)
    marks: list[tuple[int, int]] = []
    for section in index.sections:
        if section.start_page != page_no:
            continue
        needle = _loose_key(section.title)
        if not needle:
            continue
        position = key.find(needle)
        if position < 0:
            # 标题里有字母间距写法时按前 20 个字符再试一次
            position = key.find(needle[:20]) if len(needle) > 20 else -1
        if position >= 0:
            marks.append((section.index, position))
    marks.sort(key=lambda item: item[1])
    return marks


def _text_heading_sections(pages: list[str]) -> list[Section]:
    """读不到 PDF 排版时的兜底：只认原文里字面像标题的行（候选）。"""
    found: list[tuple[int, str]] = []
    for page_no, page in enumerate(pages, start=1):
        for raw_line in (page or "").splitlines():
            line = raw_line.strip()
            if not line or len(line) > 90:
                continue
            if _NUMBERED_HEADING_RE.match(line) or _PLAIN_HEADING_RE.match(line):
                if found and found[-1][1] == line:
                    continue
                found.append((page_no, line))
    if len(found) < 2:
        return []
    sections: list[Section] = []
    for position, (page_no, title) in enumerate(found):
        next_page = found[position + 1][0] if position + 1 < len(found) else len(pages)
        end = max(page_no, min(next_page - 1, len(pages))) if position + 1 < len(found) else len(pages)
        sections.append(Section(index=position + 1, title=title, start_page=page_no,
                                end_page=end, source="heading"))
    return sections


def _page_window_sections(pages: list[str], *, window: int = 4) -> list[Section]:
    sections: list[Section] = []
    total = len(pages)
    for start in range(1, total + 1, window):
        end = min(start + window - 1, total)
        sections.append(Section(index=len(sections) + 1,
                                title=f"第 {start}–{end} 页",
                                start_page=start, end_page=end, source="pages"))
    return sections


def build_index(paper: Paper, *, with_coords: bool = False) -> PaperIndex | None:
    """为这篇文献建全文索引。读不到按页正文时返回 None（调用方须如实说明）。"""
    from ..agent import evidence as evidence_mod      # 延迟导入，避免循环依赖

    pages, _reason = evidence_mod.load_pages_with_reason(paper)
    if not pages:
        return None

    index = PaperIndex(paper_id=paper.id, title=paper.title or "", page_count=len(pages),
                       pages=list(pages))

    toc = read_toc(paper_paths.resolve_local_path(paper.local_path) or Path(""))
    level1 = [(title, page) for level, title, page in toc if level <= 1]
    if len(level1) >= 2:
        index.toc_available = True
        for position, (title, page) in enumerate(level1):
            end = level1[position + 1][1] - 1 if position + 1 < len(level1) else len(pages)
            end = min(max(end, page), len(pages))
            index.sections.append(Section(index=len(index.sections) + 1, title=title,
                                          start_page=page, end_page=end, source="toc"))
        index.notes.append(f"章节来自 PDF 自带目录书签（{len(index.sections)} 章）")
    else:
        pdf_path = paper_paths.resolve_local_path(paper.local_path)
        headings = read_headings(pdf_path) if pdf_path else []
        sections = _sections_from_headings(headings, len(pages))
        if not sections:
            sections = _text_heading_sections(pages)
        if sections:
            index.sections = sections
            index.notes.append(
                f"PDF 没有目录书签，章节按原文标题的排版（字号/加粗）提取，共 "
                f"{len(sections)} 节；标题文字来自原文，未做改写"
            )
        else:
            index.sections = _page_window_sections(pages)
            index.notes.append("PDF 既没有书签也识别不到标题，按每 4 页一组划分，仅用于分批分析")

    # 整页重复出现的行（页眉"G. D. Bensen et al."、图注坐标轴那一行）只用来给段落**降权**，
    # 不删字：段落保持与原文逐字一致，定位与高亮才不会失准。
    lead_counts: dict[str, int] = {}
    for page_text in pages:
        for block in _PARA_SPLIT_RE.split(page_text or ""):
            first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
            if first_line:
                lead_counts[first_line] = lead_counts.get(first_line, 0) + 1
    repeated_leads = {line for line, count in lead_counts.items() if count >= 3}

    for page_no, page_text in enumerate(pages, start=1):
        # 同一页可能排着两个章节的标题（下一节从页中间开始），所以按**页内位置**
        # 把段落归给最近的一个标题，而不是整页都算前一节。
        marks = _heading_offsets(index, page_text, page_no)
        fallback = index.section_of_page(page_no)
        for order, (text, offset) in enumerate(_split_paragraphs_with_offsets(page_text), start=1):
            section_index = 0
            for heading_index, heading_offset in marks:
                if heading_offset <= offset:
                    section_index = heading_index
            if section_index == 0 and fallback is not None and not marks:
                section_index = fallback.index
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            index.passages.append(Passage(
                paper_id=paper.id, page=page_no, order=order,
                section_index=section_index, text=text,
                noise_lead=bool(
                    # 只认"在多个页面上以同样文字开头"的行（真正的页眉/期刊名），
                    # 不做"大写短句"这种粗判——那会把正常正文的开头也一起降权。
                    first_line and first_line in repeated_leads
                ),
            ))

    if with_coords:
        attach_coords(paper, index)
    return index


def attach_coords(paper: Paper, index: PaperIndex) -> int:
    """给段落补上真实坐标（PyMuPDF 文本层）。返回补上坐标的段落数。

    取不到坐标的页（扫描件、字体映射缺失）不写任何假坐标。
    双栏排版里整段文字在文本流中并不连续，所以先按整段定位，不中再依次用
    240 / 120 / 64 字的**段首**定位；坐标覆盖的范围始终是"命中的那段原文"。
    """
    pdf_path = paper_paths.resolve_local_path(paper.local_path)
    if pdf_path is None:
        index.notes.append("本地找不到 PDF，索引只按已解析的正文建立，无法给出坐标")
        return 0
    by_page: dict[int, list[Passage]] = {}
    for passage in index.passages:
        by_page.setdefault(passage.page, []).append(passage)

    updated: list[Passage] = []
    filled = 0
    try:
        with _fitz.open(pdf_path) as doc:           # 只读打开
            for page_no in sorted(by_page):
                if page_no - 1 >= doc.page_count:
                    continue
                page = doc[page_no - 1]
                for passage in by_page[page_no]:
                    located = None
                    for candidate in (passage.text, passage.text[:240],
                                      passage.text[:120], passage.text[:64]):
                        probe = pdf_view.locate_quote(page, candidate)
                        if probe.rects:
                            located = probe
                            break
                    if located is None:
                        updated.append(passage)
                        continue
                    if located.rects:
                        index.coord_pages.add(page_no)
                        filled += 1
                        updated.append(Passage(
                            paper_id=passage.paper_id, page=passage.page, order=passage.order,
                            section_index=passage.section_index, text=passage.text,
                            coords=located.rects, noise_lead=passage.noise_lead,
                        ))
    except Exception as exc:  # noqa: BLE001 - 打不开 PDF 也要如实说
        index.notes.append(f"PDF 打不开，段落没有坐标：{type(exc).__name__}")
        return 0
    index.passages = updated
    if filled:
        index.notes.append(
            f"{filled} 个段落的坐标来自 PDF 文本层（按段落开头定位）；"
            "其余段落没有可用文本层，不做定位"
        )
    return filled


# ---------------------------------------------------------------- 分批与检索


@dataclass(frozen=True)
class RequestChunk:
    """一次模型请求要送的正文，以及它覆盖了哪些页。"""

    title: str
    start_page: int
    end_page: int
    pages: tuple[int, ...]
    text: str
    section_indexes: tuple[int, ...] = ()

    @property
    def chars(self) -> int:
        return len(self.text)


def _excerpt_for_pages(index: PaperIndex, pages: list[int], *, budget: int) -> str:
    parts: list[str] = []
    used = 0
    for page_no in pages:
        body = (index.pages[page_no - 1] or "").strip()
        if not body:
            continue
        room = budget - used
        if room <= 0:
            break
        chunk = body[:room]
        parts.append(f"【第 {page_no} 页】\n{chunk}")
        used += len(chunk)
    return "\n\n".join(parts)


def plan_requests(index: PaperIndex, *, max_chars: int = DEFAULT_SECTION_CHARS,
                  max_requests: int = DEFAULT_MAX_REQUESTS) -> list[RequestChunk]:
    """把**整篇**论文切成有界请求序列，保证每一页都被送到。

    逐页推进（不是逐章节推进）：同一页可能同时属于两个章节（标题排在页面中途），
    逐页推进天然不会漏页也不会重复送页。块数超过 ``max_requests`` 时把相邻块合并，
    页码仍然齐全。
    """
    chunks: list[RequestChunk] = []
    current_pages: list[int] = []
    current_texts: list[str] = []
    current_used = 0
    current_meta: list[tuple[int, str, int]] = []      # (章节序号, 标题, 起始页)

    def flush() -> None:
        nonlocal current_pages, current_texts, current_used, current_meta
        if not current_pages:
            return
        title = current_meta[0][1] if len(current_meta) == 1 else " / ".join(
            dict.fromkeys(meta[1] for meta in current_meta)
        )
        chunks.append(RequestChunk(
            title=title[:120],
            start_page=current_pages[0], end_page=current_pages[-1],
            pages=tuple(current_pages), text="\n\n".join(current_texts),
            section_indexes=tuple(meta[0] for meta in current_meta),
        ))
        current_pages, current_texts, current_used, current_meta = [], [], 0, []

    for page_no in range(1, index.page_count + 1):
        body = (index.pages[page_no - 1] or "").strip()
        if not body:
            continue
        block = f"【第 {page_no} 页】\n{body}"
        if current_used + len(block) > max_chars and current_pages:
            flush()
        current_pages.append(page_no)
        current_texts.append(block)
        current_used += len(block)
        section = index.section_of_page(page_no)
        if section is None:
            continue
        if not current_meta or current_meta[-1][0] != section.index:
            current_meta.append((section.index, section.title, section.start_page))
    flush()

    if len(chunks) > max_requests:
        merged = _merge_chunks(chunks, max_requests)
        return merged
    return chunks


def _merge_chunks(chunks: list[RequestChunk], limit: int) -> list[RequestChunk]:
    """块数超上限时按顺序合并，保证仍覆盖全部页（正文按预算截断，但不丢页码）。"""
    size = math.ceil(len(chunks) / limit)
    out: list[RequestChunk] = []
    for start in range(0, len(chunks), size):
        group = chunks[start:start + size]
        out.append(RequestChunk(
            title=" / ".join(dict.fromkeys(item.title for item in group))[:120],
            start_page=group[0].start_page, end_page=group[-1].end_page,
            pages=tuple(page for item in group for page in item.pages),
            text="\n\n".join(item.text for item in group),
            section_indexes=tuple(idx for item in group for idx in item.section_indexes),
        ))
    return out


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    words = _WORD_RE.findall(lowered)
    for run in _CJK_RE.findall(lowered):
        words.extend(run[i:i + 2] for i in range(len(run) - 1))
    return words


_NOISE_TOKEN_RE = re.compile(r"^[^A-Za-z\u4e00-\u9fff]*$")


def is_prose(text: str) -> bool:
    """这段更像正文，还是像图注里的坐标轴标签？

    坐标轴标签（``time (sec) -2000 -1000 a) b)``）在检索里是纯噪声：它不含方法
    描述，却会因为术语恰好出现而排到前面。这里只做**排序过滤**，不改正文本身，
    也不影响引文定位。
    """
    value = (text or "").strip()
    if len(value) < 80:
        return False
    tokens = value.split()
    if not tokens:
        return False
    noise = sum(1 for token in tokens if _NOISE_TOKEN_RE.match(token))
    letters = sum(char.isalpha() for char in value)
    return noise / len(tokens) <= 0.30 and letters / len(value) >= 0.55


@dataclass(frozen=True)
class RetrievalHit:
    passage: Passage
    score: float
    section_title: str = ""

    @property
    def locator(self) -> str:
        return self.passage.locator


def retrieve(index: PaperIndex, query: str, *, top_k: int = 8,
             min_score: float = 0.0, prose_only: bool = True) -> list[RetrievalHit]:
    """按问题跨全文召回段落（BM25，无外部依赖）。

    返回结果带真实页码与段落号，界面可直接跳到 PDF。``prose_only=True`` 时把
    图注/坐标轴标签之类的噪声段排除在候选之外（不改正文，只是不参与排序）。
    """
    query_tokens = _tokens(query)
    if not query_tokens or not index.passages:
        return []
    candidates = [
        item for item in index.passages
        if not prose_only or is_prose(item.text)
    ]
    if not candidates:
        candidates = list(index.passages)
    doc_tokens = [_tokens(item.text) for item in candidates]
    doc_freq: dict[str, int] = {}
    for tokens in doc_tokens:
        for token in set(tokens):
            doc_freq[token] = doc_freq.get(token, 0) + 1

    total = len(candidates)
    avg_len = sum(len(item) for item in doc_tokens) / max(total, 1)
    k1, b = 1.4, 0.75
    hits: list[RetrievalHit] = []
    for position, tokens in enumerate(doc_tokens):
        if not tokens:
            continue
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        score = 0.0
        for token in set(query_tokens):
            freq = counts.get(token, 0)
            if not freq:
                continue
            idf = math.log(1 + (total - doc_freq.get(token, 0) + 0.5) /
                           (doc_freq.get(token, 0) + 0.5))
            denom = freq + k1 * (1 - b + b * len(tokens) / max(avg_len, 1))
            score += idf * freq * (k1 + 1) / denom
        section = index.section_of_page(candidates[position].page)
        title_tokens = set(_tokens(section.title)) if section else set()
        title_hits = len(title_tokens & set(query_tokens))
        if title_hits:
            score *= 1 + 0.35 * title_hits          # 命中章节标题的段落更相关
        if candidates[position].noise_lead:
            score *= 0.5                            # 以页眉/图注标签开头的段落降权（不删）
        if score > min_score:
            hits.append(RetrievalHit(
                passage=candidates[position], score=score,
                section_title=section.title if section else "",
            ))
    hits.sort(key=lambda hit: (-hit.score, hit.passage.page, hit.passage.order))
    return hits[:top_k]


def coverage(index: PaperIndex, analyzed_pages: set[int]) -> dict:
    """实际分析了多少页、哪些章节没覆盖、为什么。界面据此如实展示。"""
    analyzed = sorted(page for page in analyzed_pages if 1 <= page <= index.page_count)
    total_chars = sum(len(page or "") for page in index.pages)
    analyzed_chars = sum(len(index.pages[page - 1] or "") for page in analyzed)
    missing = [section for section in index.sections
               if not any(section.start_page <= page <= section.end_page for page in analyzed)]
    return {
        "page_count": index.page_count,
        "pages_analyzed": len(analyzed),
        "pages": analyzed,
        "chars_total": total_chars,
        "chars_analyzed": analyzed_chars,
        "coverage_ratio": round(len(analyzed) / max(index.page_count, 1), 3),
        "sections_total": len(index.sections),
        "sections_covered": len(index.sections) - len(missing),
        "sections_missing": [
            {"title": section.title, "pages": [section.start_page, section.end_page],
             "reason": "正文为空或本次未送到模型"}
            for section in missing
        ],
        "section_source": index.section_source,
        "notes": list(index.notes),
    }
