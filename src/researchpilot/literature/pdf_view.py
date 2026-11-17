"""PDF 原文阅读与引文高亮（P1-01）。

四条硬约束，全部写进实现里而不是靠调用方自觉：

1. **只读**：原文件用只读方式打开，从不 save；高亮画在"把原页复制到临时页"的副本上，
   原文件字节与 sha256 全程不变（回归里有断言）。
2. **命中才画框**：先用 PyMuPDF 的文本层做**规范化精确匹配**（忽略空白与连字符），
   命中后按"每行一个矩形"给出多个区域，跨行引文不会被压成一个不准的大框。
   匹配不到就只返回页码，不画任何高亮。
3. **不虚构定位**：中文 PDF 没有可用文本层（字体映射缺失）时，照常渲染页面，
   但定位状态是 ``no_text_layer``，一个字都不猜。
4. **缓存有界且不串项目**：键包含 PDF 内容 sha256 + 页码 + 缩放 + 引文哈希，
   落到 ``outputs/cache/pdf_pages/``，条目数超过上限时按最旧清理。

疑似转述（``Certainty.IMPLIED``）由调用方决定是否传 quote：本模块只回答
"这段文字在不在这一页、在哪儿"，不替调用方判定证据强度。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyMuPDF 1.24+ 推荐 import pymupdf；旧名字 fitz 仍可用
    import pymupdf as _fitz
except Exception:  # pragma: no cover - 兼容旧环境
    import fitz as _fitz  # type: ignore

from ..config import PATHS

CACHE_DIR = PATHS.outputs / "cache" / "pdf_pages"
RENDER_VERSION = "v1"          # 渲染/画框参数变了就 +1，避免旧缓存串色
MAX_CACHE_ENTRIES = 300        # 上限：约 300 张页图，超出按最旧清理
MIN_PARTIAL_CHARS = 12         # 部分命中的最短长度（太短的高亮没有意义）
MAX_GEOMETRY_ENTRIES = 32      # 文档几何信息的内存缓存条数（按文件）

_DASHES = "‐‑‒–—―"
# 排版差异：引文与原文之间常见的标点/引号差异。这些不改变"字面顺序一致"的事实，
# 但会让逐字比较失败——跨行引文尤其常见（原文有个冒号，引文里没有）。
_PUNCT = "，。、；：！？…·「」『』【】（）《》〈〉,.;:!?\"'`~()[]{}<>"
_SPACE_RE = re.compile(r"\s+")


def _loose(text: str) -> str:
    """定位用的规范化：统一宽度、小写、去掉空白、连字符与排版标点（**不改字**）。

    只做无损变换。PDF 里跨行的词常带连字符（`dis- persion`），去掉连字符后才能
    与条目里的引文对上；跨行处原文可能多一个冒号或逗号，也要按排版差异处理。
    判定标准是"**字符顺序完全一致，只是排版差异**"，不做模糊匹配、不做同义替换。
    """
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    for dash in _DASHES:
        value = value.replace(dash, "-")
    value = value.replace("-", "")
    for char in _PUNCT:
        value = value.replace(char, "")
    return _SPACE_RE.sub("", value)


def sha256_of(path: Path) -> str:
    """文件内容指纹。带内存缓存：连续渲染几十页时不必每页都把整份 PDF 读一遍。"""
    key = _file_key(path)
    if key is not None and key in _SHA_CACHE:
        return _SHA_CACHE[key]
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    value = digest.hexdigest()
    if key is not None:
        if len(_SHA_CACHE) >= MAX_GEOMETRY_ENTRIES:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = value
    return value


def _file_key(path: Path | str) -> tuple[str, int, int] | None:
    """(绝对路径, mtime_ns, 大小)。文件被替换时缓存自动失效。

    在 WSL 里读 `/mnt/e`（Windows 盘）的 `stat` 一次要十几毫秒，逐页渲染几十页
    会被它拖住，所以路径 → key 的换算再缓存 30 秒：文件被替换仍会在 30 秒内被发现，
    而一次连续阅读里不会有几十次 stat。
    """
    text = str(path)
    now = time.monotonic()
    cached = _KEY_CACHE.get(text)
    if cached is not None and now - cached[0] < _KEY_TTL_S:
        return cached[1]
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (text, stat.st_mtime_ns, stat.st_size)
    _KEY_CACHE[text] = (now, key)
    return key


_KEY_TTL_S = 30.0
_KEY_CACHE: dict[str, tuple[float, tuple[str, int, int]]] = {}
_SHA_CACHE: dict[tuple[str, int, int], str] = {}
_GEOMETRY_CACHE: dict[tuple[str, int, int], "DocGeometry"] = {}


@dataclass(frozen=True)
class DocGeometry:
    """整份 PDF 的几何信息：**真实文档页数**与每页尺寸（PDF 点）。

    页数只从这里来——不用"成功提取了文本的页数"，也不用"已经渲染出来的页数"，
    避免中途失败时把长文档悄悄截断。尺寸用来给每页占位，图片加载时不会跳。
    """

    ok: bool
    page_count: int = 0
    sizes: tuple[tuple[float, float], ...] = ()
    reason: str = ""

    def aspect(self, page_no: int) -> float | None:
        """第 page_no 页的宽高比（宽 / 高）。取不到返回 None。"""
        if 1 <= page_no <= len(self.sizes):
            width, height = self.sizes[page_no - 1]
            if width > 0 and height > 0:
                return width / height
        return None


def document_geometry(pdf_path: Path | str, *, use_cache: bool = True) -> DocGeometry:
    """读整份文档的页数与每页尺寸（只 open + 读 rect，不做光栅化）。"""
    path = Path(pdf_path)
    if not path.is_file():
        return DocGeometry(False, reason=f"找不到 PDF 文件（{path.name}）")
    key = _file_key(path)
    if use_cache and key is not None and key in _GEOMETRY_CACHE:
        return _GEOMETRY_CACHE[key]
    try:
        source = _fitz.open(path)
    except Exception as exc:  # noqa: BLE001 - 真实原因要给用户看
        return DocGeometry(False, reason=f"PDF 打不开：{type(exc).__name__}：{exc}")
    try:
        sizes = []
        for index in range(source.page_count):
            rect = source[index].rect
            sizes.append((float(rect.width), float(rect.height)))
        result = DocGeometry(True, page_count=source.page_count, sizes=tuple(sizes))
    except Exception as exc:  # noqa: BLE001
        return DocGeometry(False, reason=f"读取页数失败：{type(exc).__name__}：{exc}")
    finally:
        source.close()
    if key is not None:
        if len(_GEOMETRY_CACHE) >= MAX_GEOMETRY_ENTRIES:
            _GEOMETRY_CACHE.clear()
        _GEOMETRY_CACHE[key] = result
    return result


def drop_page_cache(pdf_path: Path | str, page_no: int | None = None, *, zoom: float | None = None) -> int:
    """丢掉某份 PDF 的页图缓存（渲染失败时的"重试"用）。返回删除的文件数。"""
    try:
        digest = sha256_of(pdf_path)
    except OSError:
        return 0
    removed = 0
    pattern = f"{digest[:16]}__"
    if page_no is not None:
        pattern += f"p{int(page_no):04d}__"
    for png in CACHE_DIR.glob(f"{pattern}*.png"):
        if zoom is not None and f"__z{zoom:g}__" not in png.name:
            continue
        for sidecar in (png, png.with_suffix(".json")):
            try:
                sidecar.unlink()
                removed += 1
            except OSError:
                continue
    return removed


@dataclass(frozen=True)
class LocateResult:
    """引文在这一页的定位结果。``rects`` 是**每行一个矩形**（可能多个）。"""

    status: str                     # exact | partial | none | no_text_layer | out_of_range
    rects: tuple[tuple[float, float, float, float], ...] = ()
    detail: str = ""

    @property
    def highlighted(self) -> bool:
        return self.status in {"exact", "partial"} and bool(self.rects)


@dataclass(frozen=True)
class PageRender:
    ok: bool
    page: int                       # 1 起
    page_count: int
    zoom: float
    png: bytes | None = None
    locate: LocateResult = field(default_factory=lambda: LocateResult("none"))
    reason: str = ""                # ok=False 时的真实原因
    from_cache: bool = False


def _word_map(page) -> tuple[str, list[int], list[tuple]]:
    """把一页的文本压成"规范化字符序列 + 每个字符属于哪个词"。"""
    words = page.get_text("words")
    chars: list[str] = []
    owner: list[int] = []
    for index, item in enumerate(words):
        token = _loose(item[4])
        for char in token:
            chars.append(char)
            owner.append(index)
    return "".join(chars), owner, words


def _rects_from_words(words: list[tuple], indices: list[int]) -> tuple[tuple[float, float, float, float], ...]:
    """把覆盖到的词按 (block, line) 分行，每行取并集矩形 → 每行一个高亮区域。"""
    lines: dict[tuple, list[tuple]] = {}
    for word_index in sorted(set(indices)):
        item = words[word_index]
        key = (item[5], item[6])          # block_no, line_no
        lines.setdefault(key, []).append(item)
    rects = []
    for key in sorted(lines, key=lambda k: (lines[k][0][1], lines[k][0][0])):
        group = lines[key]
        x0 = min(float(w[0]) for w in group)
        y0 = min(float(w[1]) for w in group)
        x1 = max(float(w[2]) for w in group)
        y1 = max(float(w[3]) for w in group)
        rects.append((x0, y0, x1, y1))
    return tuple(rects)


def locate_quote(page, quote: str) -> LocateResult:
    """在这一页里定位引文。命中→每行一个矩形；未命中→说明原因，不猜坐标。"""
    text, owner, words = _word_map(page)
    if not text:
        return LocateResult("no_text_layer", detail="这一页没有可用的文本层（扫描件或字体映射缺失）")
    needle = _loose(quote)
    if not needle:
        return LocateResult("none", detail="没有可用的引文文本，无法定位")

    start = text.find(needle)
    if start >= 0:
        rects = _rects_from_words(words, owner[start:start + len(needle)])
        return LocateResult("exact", rects, f"逐字命中（{len(rects)} 行）")

    # 跨行/连字符之外仍可能因页码里的文字与引文略有出入：退一步只高亮前半段，
    # 并明确标成"部分命中"，不允许把它说成逐字命中。
    for fraction in (0.8, 0.6, 0.45):
        cut = int(len(needle) * fraction)
        if cut < MIN_PARTIAL_CHARS:
            continue
        start = text.find(needle[:cut])
        if start >= 0:
            rects = _rects_from_words(words, owner[start:start + cut])
            return LocateResult(
                "partial", rects,
                f"部分命中（引文与原文存在差异，已按前 {int(fraction * 100)}% 定位）",
            )
    return LocateResult("none", detail="这一页的文本层里找不到该引文，只跳页不做高亮")


def _draw_highlight(page, rects: tuple[tuple[float, float, float, float], ...]) -> None:
    """在**临时副本页**上画半透明高亮。原文件从不参与写入。"""
    for x0, y0, x1, y1 in rects:
        rect = _fitz.Rect(x0 - 1.0, y0 - 1.5, x1 + 1.0, y1 + 1.5)
        page.draw_rect(
            rect, color=(0.95, 0.75, 0.10), fill=(1.0, 0.87, 0.25),
            fill_opacity=0.38, width=0.6,
        )


def _cache_paths(pdf_sha: str, page_no: int, zoom: float, quote: str) -> tuple[Path, Path]:
    quote_hash = hashlib.sha256(_loose(quote).encode("utf-8")).hexdigest()[:12]
    stem = f"{pdf_sha[:16]}__p{page_no:04d}__z{zoom:g}__q{quote_hash}__{RENDER_VERSION}"
    return CACHE_DIR / f"{stem}.png", CACHE_DIR / f"{stem}.json"


def _prune_cache(limit: int = MAX_CACHE_ENTRIES) -> int:
    """按最旧清理，保证缓存不会无限增长。返回删除的文件数。"""
    if not CACHE_DIR.is_dir():
        return 0
    files = sorted(CACHE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    removed = 0
    for path in files[: max(0, len(files) - limit)]:
        for sidecar in (path, path.with_suffix(".json")):
            try:
                sidecar.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def cache_stats() -> dict:
    if not CACHE_DIR.is_dir():
        return {"entries": 0, "bytes": 0, "limit": MAX_CACHE_ENTRIES}
    files = list(CACHE_DIR.glob("*.png"))
    return {
        "entries": len(files),
        "bytes": sum(item.stat().st_size for item in files),
        "limit": MAX_CACHE_ENTRIES,
    }


def clear_cache() -> int:
    """清空缓存（测试与排错用）。返回删除的页图数。"""
    if not CACHE_DIR.is_dir():
        return 0
    count = 0
    for path in CACHE_DIR.glob("*.png"):
        for sidecar in (path, path.with_suffix(".json")):
            try:
                sidecar.unlink()
                count += 1
            except OSError:
                continue
    return count


def render_page(
    pdf_path: Path | str,
    page_no: int,
    *,
    quote: str = "",
    zoom: float = 1.0,
    use_cache: bool = True,
) -> PageRender:
    """渲染第 ``page_no`` 页（1 起）并对引文做定位高亮。

    任何失败都返回 ``ok=False`` + 真实原因，不抛异常、不返回半成品。
    """
    path = Path(pdf_path)
    zoom = max(0.5, min(3.0, float(zoom or 1.0)))
    if not path.is_file():
        return PageRender(False, page_no, 0, zoom, reason=f"找不到 PDF 文件（{path.name}）")

    pdf_sha = sha256_of(path)
    png_path, meta_path = _cache_paths(pdf_sha, page_no, zoom, quote)
    if use_cache and png_path.is_file() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            locate = LocateResult(
                meta.get("status", "none"),
                tuple(tuple(r) for r in meta.get("rects", [])),
                meta.get("detail", ""),
            )
            return PageRender(
                True, page_no, int(meta.get("page_count", 0)), zoom,
                png_path.read_bytes(), locate, from_cache=True,
            )
        except (OSError, ValueError):
            pass       # 缓存坏了就重画，不让它成为故障点

    try:
        source = _fitz.open(path)          # 只读打开；从不 save
    except Exception as exc:  # noqa: BLE001 - 真实原因要显示给用户
        return PageRender(False, page_no, 0, zoom, reason=f"PDF 打不开：{type(exc).__name__}：{exc}")

    try:
        page_count = source.page_count
        if not (1 <= page_no <= page_count):
            return PageRender(
                False, page_no, page_count, zoom,
                reason=f"页码超出范围：这份 PDF 共 {page_count} 页，请求的是第 {page_no} 页",
            )
        source_page = source[page_no - 1]
        locate = locate_quote(source_page, quote) if quote else LocateResult("none", detail="这条证据没有引文")

        # 关键：把原页复制到一张**临时**页上，高亮只画在副本上
        temp = _fitz.open()
        rect = source_page.rect
        temp_page = temp.new_page(width=rect.width, height=rect.height)
        temp_page.show_pdf_page(temp_page.rect, source, page_no - 1)
        if locate.highlighted:
            _draw_highlight(temp_page, locate.rects)
        pixmap = temp_page.get_pixmap(matrix=_fitz.Matrix(zoom, zoom))
        png = pixmap.tobytes("png")
        temp.close()
    except Exception as exc:  # noqa: BLE001
        return PageRender(False, page_no, 0, zoom, reason=f"渲染失败：{type(exc).__name__}：{exc}")
    finally:
        source.close()

    if use_cache:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            png_path.write_bytes(png)
            meta_path.write_text(json.dumps({
                "status": locate.status, "rects": [list(r) for r in locate.rects],
                "detail": locate.detail, "page_count": page_count,
                "zoom": zoom, "quote_chars": len(quote),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False), encoding="utf-8")
            _prune_cache()
        except OSError:
            pass        # 缓存写不进去不影响这次阅读

    return PageRender(True, page_no, page_count, zoom, png, locate)
