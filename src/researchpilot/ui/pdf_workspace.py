"""双栏独立阅读工作区：左边连续翻阅整篇 PDF，右边独立阅读/编辑科研内容。

**两个区域完全独立**：各自有滚动条、各自记住滚动位置；左边滚不会动右边，
右边滚不会动左边；没有跟随、没有按比例同步、没有语义锚点自动联动。

唯一的联动是**显式点击证据**：右侧点「PDF · 第 N 页」→ 左侧主动跳到那一页并高亮
（有真实坐标才画框）。这是两个不同操作，代码里也是两条路径：

- 滚动：纯浏览器行为，`scroll_keeper` 只负责跨 rerun 记住位置，不通知 Python；
- 点击：一次明确的 Python 事件，写入阅读请求，只影响左侧。

实现要点（针对"整页被滚走/位置被重置"这两个真实问题）：

1. 左右各用一个 `st.container(height=...)` —— Streamlit 官方的固定高度滚动容器，
   滚轮落在哪一侧就只滚那一侧，不会再带动外层页面；
2. 左侧按批次连续渲染页面（不是只显示一页），批次与页码存在会话里；
3. 两个容器内各放一个 `scroll_keeper` 组件，把 scrollTop 存到浏览器里，
   rerun、编辑备忘、拖动分隔线之后都能回到原来的位置。
"""

from __future__ import annotations

import logging

import streamlit as st

from ..literature import paths as paper_paths
from ..literature import pdf_view
from ..storage.models import Paper, Project
from . import browser_prefs, pane_tools, scroll_keeper, split_layout, theme

log = logging.getLogger(__name__)

WORKBENCH = "pdf"
STATE_KEY = "pdf_workspace"
COLLAPSED_KEY = "pdf_workspace_collapsed"
DEFAULT_WIDTH = 540              # 1440 视口下约主工作区 45%
# 分隔条的 Python 高度只是"下限"：可视高度由 CSS 拉伸到 --rp-read-h。
# 给大了会把整行撑高（曾经固定 620px，阅读页因此永远比视口高出约 100px）。
PANE_HEIGHT = 32
# 连续阅读：一次渲染多少页、每次"滚到底"再补多少页。
# 不是上限——窗口会一直长到真实页数为止（`PAGES_PER_BATCH` 曾固定为 6，
# 这正是"只能读到第六页"的根因：第 7 页以后从来没有被渲染过）。
WINDOW_FIRST = 24                # 首次渲染页数（多数论文一次就够）
WINDOW_STEP = 24                 # 滚到底之后每次追加的页数
WINDOW_MAX = 60                  # 首次渲染的上限：更长的文档走"滚到底自动补"
DEFAULT_ZOOM = 1.2
ZOOM_CHOICES = {"1.2×": 1.2, "1.5×": 1.5, "2×": 2.0}

_STATUS = {
    "exact": ("逐字命中并高亮", "evidence"),
    "partial": ("部分命中（引文与原文有差异）", "warn"),
    "none": ("未命中，仅跳页", "warn"),
    "no_text_layer": ("无文本层，仅显示原页", "warn"),
}


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def _state() -> dict | None:
    value = st.session_state.get(STATE_KEY)
    return value if isinstance(value, dict) else None


def current(project_id: str) -> dict | None:
    """当前阅读请求；不属于这个项目时返回 None（项目隔离）。"""
    value = _state()
    if not value or value.get("project_id") != project_id:
        return None
    return value


def open_document(project_id: str, paper_id: str, page: int | None = None,
                  quote: str = "", *, verified: bool = True) -> None:
    """打开/切换左侧 PDF：显式跳转（由证据按钮触发），会重排页面批次。"""
    target = int(page or 1)
    # 每次请求都递增 seq：阅读页据此把窗口挪到目标页。
    # 旧实现读的是 `pdf_seq` 却从没写过它，于是 seq 永远是 1，
    # "同一个会话里再点一次定位"就不会生效（浏览器验收抓到过）。
    seq = int(st.session_state.get("pdf_ws_seq", 0) or 0) + 1
    st.session_state["pdf_ws_seq"] = seq
    st.session_state[STATE_KEY] = {
        "project_id": project_id,
        "paper_id": paper_id,
        "page": target,
        "quote": quote or "",
        "verified": bool(verified),
        "batch": _batch_for(target),
        "seq": seq,
    }
    st.session_state[COLLAPSED_KEY] = False


def _batch_for(page: int) -> int:
    """让目标页落在批次的第一页，跳过去就能直接看到。"""
    return max(1, int(page))


def close() -> None:
    st.session_state.pop(STATE_KEY, None)


def set_collapsed(collapsed: bool) -> None:
    st.session_state[COLLAPSED_KEY] = bool(collapsed)


def is_collapsed() -> bool:
    return bool(st.session_state.get(COLLAPSED_KEY, False))


def visible(project_id: str) -> bool:
    """是否渲染双栏：有阅读请求、没被收起、且窗口够宽（窄窗口退单栏）。"""
    if current(project_id) is None or is_collapsed():
        return False
    return not browser_prefs.viewport_is_narrow()


def papers_with_pdf(project: Project) -> list[Paper]:
    from ..storage import repository as repo   # 局部导入，避免与 repository 成环

    return [
        paper for paper in repo.list_all(Paper, project.id)
        if paper_paths.resolve_local_path(paper.local_path)
    ]


# ---------------------------------------------------------------------------
# 左侧：连续阅读
# ---------------------------------------------------------------------------


def _page_image(path, page: int, quote: str, zoom: float):
    """渲染一页；只有目标页带引文（其余页不带引文，避免无谓的重算）。"""
    result = pdf_view.render_page(path, page, quote=quote, zoom=zoom)
    return result


def _goto_page(page: int) -> None:
    """翻页/跳页：把左栏滚到那一页（页面已经全部在栏内，不再移动窗口）。"""
    st.session_state["pdf_ws_focus_page"] = max(1, int(page))
    st.session_state["pdf_ws_focus_seq"] = int(st.session_state.get("pdf_ws_focus_seq", 0)) + 1


def focus_page() -> int:
    return int(st.session_state.get("pdf_ws_focus_page") or 1)


def scroll_request() -> tuple[str, str]:
    """(锚点, token)。token 变化才滚动，避免每次 rerun 把用户拽回去。"""
    page = focus_page()
    seq = int(st.session_state.get("pdf_ws_focus_seq", 0) or 0)
    return f"page-{page}", f"focus:{seq}:{page}"


def _reading_header(project: Project, papers: list[Paper], paper: Paper,
                    page_count: int, quote_page: int | None,
                    window: int = 0, header_extra=None) -> None:
    """阅读页顶部固定的两行。

    第一行：当前论文标题（+ 年份/期刊/DOI 这类元数据）与主要操作；
    第二行：上一页 / 下一页、页码、缩放、定位状态。

    两行都放在 key 容器里，由 theme 的 `rp_read_header` 固定在页面顶部；
    下面两个滚动容器各滚各的，这两行不跟着动。
    """
    with st.container(key="rp_read_header"):
        with st.container(key="rp_read_top"):
            first = st.columns([6, 2, 2, 2])
            meta = [item for item in (paper.year,) if item]
            if paper.doi:
                meta.append(f"DOI {paper.doi}")
            first[0].markdown(
                f'<div class="rp-read-title">{theme.esc((paper.title or "未命名文献")[:80])}</div>'
                f'<div class="rp-read-meta">{theme.esc(" · ".join(meta) or "（未核实元数据）")}</div>',
                unsafe_allow_html=True,
            )
            if len(papers) > 1:
                labels = {
                    item.id: (item.title or "未命名文献")[:52] for item in papers
                }
                ids = list(labels)
                with first[1]:
                    chosen = st.selectbox(
                        "切换论文", ids,
                        index=ids.index(paper.id) if paper.id in ids else 0,
                        format_func=lambda pid: labels[pid],
                        key="pdf_ws_switch", label_visibility="collapsed",
                    )
                    if chosen != paper.id:
                        open_document(project.id, chosen, 1, "")
                        st.rerun()
            if header_extra is not None:
                header_extra(first[2])
            if first[3].button(
                "收起原文", key="pdf_ws_collapse_top", icon=":material/close_fullscreen:",
                help="收起左侧原文，右侧内容占满整宽", width="stretch",
            ):
                set_collapsed(True)
                st.rerun()

        with st.container(key="rp_read_tools"):
            row = st.columns([1, 1, 1, 2, 2, 3])
            page_now = focus_page()
            if row[0].button(
                "上一页", key="pdf_ws_prev", disabled=page_now <= 1,
                icon=":material/navigate_before:", width="stretch",
            ):
                _goto_page(page_now - 1)
                st.rerun()
            if row[1].button(
                "下一页", key="pdf_ws_next", disabled=page_now >= page_count,
                icon=":material/navigate_next:", width="stretch",
            ):
                _goto_page(page_now + 1)
                st.rerun()
            with row[2]:
                # 页码控件的值要和"当前定位页"同步：控件带 key 会留住旧值，
                # 但**只在定位页从别处变过时**才写回——否则会把用户刚输入的页码盖掉
                # （上一轮就踩过这个坑：输入 1 之后立刻被同步回 50）。
                if st.session_state.get("pdf_ws_jump_synced") != page_now:
                    st.session_state["pdf_ws_jump"] = page_now
                    st.session_state["pdf_ws_jump_synced"] = page_now
                jump_default = (
                    {} if "pdf_ws_jump" in st.session_state else {"value": page_now}
                )
                jump = st.number_input(
                    "页码", min_value=1, max_value=page_count, step=1,
                    key="pdf_ws_jump", label_visibility="collapsed",
                    **jump_default,
                )
                if int(jump) != page_now:
                    _goto_page(int(jump))
                    st.rerun()
            with row[3]:
                st.selectbox(
                    "缩放", list(ZOOM_CHOICES),
                    index=list(ZOOM_CHOICES).index(
                        st.session_state.get("pdf_ws_zoom", "1.2×")),
                    key="pdf_ws_zoom", label_visibility="collapsed",
                )
            with row[4]:
                # 实时页码由组件自己更新（跟着滚动变），不触发重跑
                pane_tools.mount_page_readout("pdf", page_count)
            with row[5]:
                status = []
                if quote_page:
                    status.append((f"已定位到第 {quote_page} 页", "evidence"))
                if window and window < page_count:
                    status.append((f"已渲染 {window}/{page_count} 页", ""))
                if status:
                    theme.markdown(theme.chip_row(status))
                if quote_page and st.button(
                    "取消定位高亮", key="pdf_ws_clear_quote", width="stretch"
                ):
                    request = current(project.id) or {}
                    open_document(project.id, paper.id, int(request.get("page") or page_now), "")
                    st.rerun()
        # 量一次实际高度：视口 − （顶栏 + 两行工具）的真实底边
        pane_tools.mount_reading_height()


def window_for(store, paper_id: str, page_count: int) -> int:
    """渲染窗口（纯逻辑，store 是可写映射——测试直接传 dict）。

    窗口只增不减；换论文时重置。**不是上限**：一直可以长到真实页数。
    """
    if store.get("pdf_ws_window_paper") != paper_id:
        store["pdf_ws_window_paper"] = paper_id
        store["pdf_ws_window"] = min(page_count, WINDOW_MAX)
    window = int(store.get("pdf_ws_window") or 0)
    window = max(min(page_count, WINDOW_FIRST), min(window, page_count))
    store["pdf_ws_window"] = window
    return window


def extend_window_for(store, paper_id: str, page_count: int) -> int:
    """再渲染一批（滚到底自动调用，也有手动按钮兜底）。返回新的窗口。"""
    current = window_for(store, paper_id, page_count)
    window = min(page_count, current + WINDOW_STEP)
    store["pdf_ws_window"] = window
    return window


def loaded_pages(paper_id: str, page_count: int) -> int:
    """当前已经渲染到第几页（会话状态版）。"""
    return window_for(st.session_state, paper_id, page_count)


def extend_window(paper_id: str, page_count: int) -> int:
    return extend_window_for(st.session_state, paper_id, page_count)


def _page_placeholder_css(geometry, window: int) -> None:
    """给每页占一个固定高度（按真实页面宽高比），图片到位时不会把版面顶下去。"""
    rules = []
    for page in range(1, window + 1):
        aspect = geometry.aspect(page) if geometry else None
        if not aspect:
            continue
        rules.append(f".st-key-rp_page_{page} img{{aspect-ratio:{aspect:.4f};}}")
    if rules:
        st.markdown(f"<style>{''.join(rules)}</style>", unsafe_allow_html=True)


def _page_block(project: Project, paper: Paper, path, page: int, page_count: int,
                target_page: int, quote: str, zoom: float) -> None:
    """一页：锚点 + 页码 + 图片；目标页额外画高亮并显示定位状态。

    渲染失败时给出真实原因与"重试"（只丢这一页的缓存），绝不静默跳过——
    静默跳过会让人以为文档只有这么长。
    """
    with st.container(key=f"rp_page_{page}"):
        # 零高度锚点：滚动定位与"当前页"读数都靠它
        theme.markdown(f'<div data-rp-anchor="page-{page}" class="rp-page-anchor"></div>')
        result = _page_image(path, page, quote if page == target_page else "", zoom)
        if not result.ok:
            theme.markdown(theme.chip_row([(f"第 {page} 页渲染失败", "danger")]))
            st.caption(result.reason)
            if st.button(f"重试第 {page} 页", key=f"pdf_retry_{paper.id}_{page}"):
                pdf_view.drop_page_cache(path, page)
                st.rerun()
            return
        if page == target_page:
            label, kind = _STATUS.get(result.locate.status,
                                      (result.locate.detail or "未定位", "warn"))
            chips = [(f"第 {page} / {page_count} 页", ""), (label, kind)]
            if result.locate.highlighted:
                chips.append((f"高亮 {len(result.locate.rects)} 处", "evidence"))
            theme.markdown(theme.chip_row(chips))
            if quote and not result.locate.highlighted:
                st.caption("这一页没有画出高亮：找不到该引文（扫描件无文本层 / 转述 / 引文在别的页）。")
        else:
            st.caption(f"第 {page} / {page_count} 页")
        st.image(result.png, width="stretch")


def _pages(project: Project, papers: list[Paper], paper: Paper, path,
           page_count: int, target_page: int, quote: str, zoom: float) -> None:
    """左栏：从第 1 页开始连续渲染到最后一页（长文档按需补），自己的滚动条。"""
    geometry = pdf_view.document_geometry(path)
    total = geometry.page_count or page_count          # 真实页数优先
    window = loaded_pages(paper.id, total)
    with st.container(key="rp_pdf_scroll"):
        scroll_keeper.mark("pdf")
        if window < total:
            cols = st.columns([3, 2])
            cols[0].caption(f"已渲染 {window} / {total} 页，继续向下滚动会自动加载后面的页")
            if cols[1].button(
                f"加载后面的 {min(WINDOW_STEP, total - window)} 页",
                key=f"pdf_more_{paper.id}", width="stretch",
            ):
                extend_window(paper.id, total)
                st.rerun()
        _page_placeholder_css(geometry, window)
        for page in range(1, window + 1):
            _page_block(project, paper, path, page, total, target_page, quote, zoom)
        # 定位请求/翻页请求：滚到目标页（每个 token 只滚一次）
        anchor, token = scroll_request()
        pane_tools.mount_scroll_to("pdf", anchor, token)
        if window < total:
            theme.markdown(
                f'<div class="rp-guide-note">还有 {total - window} 页没有渲染；'
                "继续向下滚动即会加载。</div>"
            )
            # 滚到附近就自动补下一批（每次扩展一个 token，不会反复触发）
            pane_tools.mount_load_sentinel(
                "pdf", f"{paper.id}:{window}", window,
                on_hit=lambda: _on_load_more(paper.id, total),
            )


def _on_load_more(paper_id: str, page_count: int) -> None:
    """哨兵命中：窗口再长一批。只改窗口，不动阅读位置。"""
    extend_window(paper_id, page_count)


# ---------------------------------------------------------------------------
# 右侧：内容（自己的滚动条 + 自己的位置）
# ---------------------------------------------------------------------------


def _content_column(content_render) -> None:
    with st.container(key="rp_content_scroll"):
        scroll_keeper.mark("content")
        content_render()


def render(
    project: Project,
    content_render,
    *,
    header_extra=None,
    missing_pdf_render=None,
) -> None:
    """阅读页：顶部两行固定，下面左右两栏各自独立滚动。

    - 左栏：论文原文（按批次连续渲染，自己的滚动条）；
    - 右栏：调用方传来的内容（文献工作台传 AI 解读，规划/成果页传各自内容）；
    - 顶部两行钉在页面顶部，**不随任何一栏滚动**；
    - 两栏高度按视口算（theme 的 `--rp-read-h`），避免底部内容被遮挡或整页滚动。
    """
    papers = papers_with_pdf(project)
    if visible(project.id):
        request = current(project.id)
        paper = next((item for item in papers if item.id == request["paper_id"]), None)
        if paper is None:
            # Missing-PDF papers are intentionally absent from papers_with_pdf().
            # Keep the record so the caller can offer its title link and uploader.
            from ..storage import repository as repo

            candidate = repo.get(Paper, str(request.get("paper_id") or ""))
            if candidate is not None and candidate.project_id == project.id:
                paper = candidate
        path = paper_paths.resolve_local_path(paper.local_path) if paper else None
        if paper is None or path is None:
            if missing_pdf_render is not None:
                missing_pdf_render(project, paper)
                return
            theme.empty_state("当前来源不提供本地 PDF")
            content_render()
            return
        probe = pdf_view.render_page(path, int(request.get("page") or 1), zoom=DEFAULT_ZOOM)
        # 真实页数以文档几何为准（不是"渲染了多少页"或"提取了多少页文本"）
        geometry = pdf_view.document_geometry(path)
        page_count = geometry.page_count or probe.page_count or 1
        requested_page = max(1, min(int(request.get("page") or 1), page_count))
        # 定位请求（点「定位原文」/证据按钮）或换论文时，把"当前定位页"设到目标页；
        # 用户自己滚到别处不会被拽回来（滚动位置只在 token 变化时被设置）。
        new_request = (
            st.session_state.get("pdf_ws_focus_paper") != paper.id
            or request.get("seq") != st.session_state.get("pdf_ws_seen")
        )
        if new_request:
            st.session_state["pdf_ws_focus_page"] = requested_page
            st.session_state["pdf_ws_focus_seq"] = int(
                st.session_state.get("pdf_ws_focus_seq", 0)
            ) + 1
        st.session_state["pdf_ws_focus_paper"] = paper.id
        st.session_state["pdf_ws_seen"] = request.get("seq")
        log.info(
            "阅读页：论文=%s 页数=%s 定位页=%s 引文=%s 新请求=%s",
            paper.id, page_count, focus_page(), bool(request.get("quote")), new_request,
        )
        zoom_label = st.session_state.get("pdf_ws_zoom", "1.2×")
        zoom = ZOOM_CHOICES.get(zoom_label, DEFAULT_ZOOM)
        quote = request.get("quote") or ""
        quote_page = int(request.get("page") or 1) if quote else None
        window = loaded_pages(paper.id, page_count)      # 同时把窗口写进会话状态
        _reading_header(project, papers, paper, page_count, quote_page,
                        window=window, header_extra=header_extra)
        split_layout.render(
            workbench=WORKBENCH,
            index_title="论文原文",
            index_render=lambda: _pages(project, papers, paper, path, page_count,
                                        int(request.get("page") or 1), quote, zoom),
            detail_render=lambda: _content_column(content_render),
            index_height=PANE_HEIGHT,
            index_width_default=DEFAULT_WIDTH,
            show_header=False,
        )
        return

    request = current(project.id)
    if request is not None and papers:
        # 收起原文时顶部仍然固定：只保留一行（标题 + 展开），阅读区占满整宽。
        paper = next((item for item in papers if item.id == request.get("paper_id")), None)
        title = (paper.title if paper else "") or "当前论文"
        with st.container(key="rp_read_header"):
            head = st.columns([8, 2, 2])
            head[0].markdown(
                f'<div class="rp-read-title">{theme.esc(title[:78])}</div>'
                f'<div class="rp-read-meta">原文已收起；右侧内容占满整宽</div>',
                unsafe_allow_html=True,
            )
            if head[1].button(
                "展开原文", key="pdf_ws_expand", icon=":material/open_in_full:",
                width="stretch",
            ):
                set_collapsed(False)
                st.rerun()
            if head[2].button(
                "退出阅读", key="pdf_ws_close", icon=":material/close:", width="stretch"
            ):
                close()
                st.rerun()
    content_render()
