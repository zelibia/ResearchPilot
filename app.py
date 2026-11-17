"""ResearchPilot AI —— Streamlit 入口。

启动：streamlit run app.py

结构约定：
- 本文件只负责初始化、项目选择与页面路由，不放业务逻辑。
- 当前对外只提供一个「文献阅读」工作区，页面在 src/researchpilot/ui/ 下。
- 所有持久化都经 src/researchpilot/storage/。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from researchpilot.config import (  # noqa: E402
    APP_NAME,
    APP_VERSION,
    LLM,
    PATHS,
    db_identity,
    dev_mode_enabled,
    dev_token,
    setup_logging,
)
from researchpilot.literature.papers import pymupdf_status  # noqa: E402
from researchpilot.literature import library  # noqa: E402
from researchpilot.storage import database  # noqa: E402
from researchpilot.storage import repository as repo  # noqa: E402
from researchpilot.storage.models import Paper, PaperClaim, Project  # noqa: E402
from researchpilot.ui import (  # noqa: E402
    assistant,
    browser_prefs,
    literature_page,
    llm_settings,
    outcomes_page,
    pdf_workspace,
    planner_page,
    theme,
)

log = setup_logging()
st.set_page_config(
    page_title=APP_NAME,
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="auto",
)
theme.inject()

# 当前项目写进 URL 查询参数。工作区只剩文献阅读，旧 view 值统一回落到 literature。
PROJECT_QUERY_KEY = "project"
VIEW_QUERY_KEY = "view"
CITE_QUERY_KEY = "cite_claim"
CITE_PAGE_QUERY_KEY = "cite_page"
LITERATURE_VIEW_LABEL = "文献阅读"
LITERATURE_VIEW_CODE = "literature"

# 科研配色：低饱和、可区分，不把颜色当成状态含义。
PROJECT_SWATCH_COLORS = (
    "#2F6B67",  # 深青绿
    "#315F8C",  # 学术蓝
    "#665884",  # 灰紫
    "#8A6537",  # 赭金
    "#4F704B",  # 苔绿
    "#8A4F4B",  # 砖红
    "#3F6E76",  # 蓝绿
    "#70544A",  # 棕褐
)

# 开发者诊断模式的 URL 开关，见 docs/00_项目启动/界面规范_v1.0.md
DEV_QUERY_KEY = "dev"


@st.cache_resource
def _ensure_schema() -> dict:
    """启动时把数据库补齐到当前 schema，并记录版本。

    connect() 本来就会建缺失的表，但它不写版本记录，于是"这台机器迁到哪一版"
    无从查起。这里显式跑一次 migrate()：每个服务进程只跑一次（cache_resource），
    不会在每次页面刷新时重复执行。
    """
    report = database.migrate(note="应用启动时自动补齐")
    log.info(
        "数据库 schema %s：新增表 %s",
        report["schema_version"],
        report["created_tables"] or "无",
    )
    return report


def _query_project_id() -> str:
    """URL 里的项目 ID。空、多值、非字符串一律当作没给。"""
    raw = st.query_params.get(PROJECT_QUERY_KEY)
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return raw if isinstance(raw, str) else ""


def _query_param(key: str) -> str:
    raw = st.query_params.get(key)
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return raw if isinstance(raw, str) else ""


def _consume_cite_claim(projects: list[Project]) -> None:
    """Route an inline citation to the existing PDF workspace, once."""
    claim_id = _query_param(CITE_QUERY_KEY)
    requested_page = _query_param(CITE_PAGE_QUERY_KEY)
    from_query = bool(claim_id)
    if not claim_id:
        pending = browser_prefs.get("pdf_cite")
        if isinstance(pending, dict):
            claim_id = str(pending.get("claim_id") or "")
            requested_page = str(pending.get("page") or "")
    if not claim_id:
        return
    consumed = f"{claim_id}:{requested_page}"
    if st.session_state.get("_consumed_cite_claim") == consumed:
        return

    requested_project = _query_param(PROJECT_QUERY_KEY)
    claim = repo.get(PaperClaim, claim_id)
    paper = repo.get(Paper, claim.paper_id) if claim is not None else None
    valid = bool(
        claim
        and paper
        and claim.paper_id == paper.id
        and claim.project_id == paper.project_id
        and (not requested_project or requested_project == claim.project_id)
    )
    if not valid:
        log.warning(
            "引用定位请求未通过项目/论文校验：claim=%s paper=%s project=%s",
            claim_id,
            claim.paper_id if claim else "",
            requested_project,
        )
        st.query_params.pop(CITE_QUERY_KEY, None)
        st.query_params.pop(CITE_PAGE_QUERY_KEY, None)
        return

    project = next(
        (item for item in projects if item.id == claim.project_id), None
    )
    if project is None:
        st.query_params.pop(CITE_QUERY_KEY, None)
        st.query_params.pop(CITE_PAGE_QUERY_KEY, None)
        return

    page = claim.page
    raw_page = requested_page
    if raw_page.isdigit():
        candidate_page = int(raw_page)
        if candidate_page > 0 and (
            not paper.page_count or candidate_page <= paper.page_count
        ):
            page = candidate_page
    if not page:
        pages = [
            int(value)
            for value in re.findall(r"第\s*(\d+)\s*页", claim.locator or "")
        ]
        page = next((value for value in pages if value > 0), 1)

    st.session_state["project_picker"] = project.id
    st.session_state["selected_paper_id"] = paper.id
    st.session_state["selected_paper_project"] = project.id
    st.session_state.pop("workbench_picker", None)
    pdf_workspace.open_document(
        project.id,
        paper.id,
        page,
        claim.quote if claim.quote_verified else "",
        verified=claim.quote_verified,
    )
    st.session_state["_consumed_cite_claim"] = consumed
    if from_query:
        # Raw Markdown links arrive as a transient query state. Hand the target
        # to the browser-prefs component so the follow-up rerun can restore it.
        browser_prefs.set(pdf_cite={"claim_id": claim.id, "page": page})
    else:
        # The pending target is one-shot; do not reopen it on a future visit.
        browser_prefs.set(pdf_cite={})
        st.rerun()
    log.info(
        "引用定位：claim=%s paper=%s project=%s page=%s",
        claim.id,
        paper.id,
        project.id,
        page,
    )
    browser_prefs.set(recent_project=project.id, recent_view=LITERATURE_VIEW_CODE)


def _switch_project(project_id: str) -> None:
    """侧栏点项目行切换项目。

    必须走 on_click 回调（回调在控件实例化之前执行）：在按钮的 if 分支里改
    `project_picker` 会撞 Streamlit 的 "cannot be modified after the widget ...
    is instantiated"。选中的文献是按项目隔离的，切项目时一并作废。
    """
    if not project_id:
        return
    st.session_state["project_picker"] = project_id
    st.session_state.pop("selected_paper_id", None)
    st.session_state.pop("selected_paper_project", None)
    st.query_params[PROJECT_QUERY_KEY] = project_id
    browser_prefs.set(recent_project=project_id)
    log.info("切换项目：%s", project_id)


def _return_from_graph_navigation() -> None:
    st.session_state.pop("_graph_navigation", None)
    st.session_state["_detail_default_tab"] = "引用"
    st.query_params[VIEW_QUERY_KEY] = LITERATURE_VIEW_CODE


def _dev_mode() -> bool:
    """开发者诊断模式：只认服务端授权，默认关闭。

    界面规范第 1 条要求用户界面不出现开发内容；这些内容不是删掉，而是收进这里。
    但诊断面板会暴露数据库路径、原始数据目录等内部信息，**不能只凭公开 URL 打开**：

    1. `RESEARCHPILOT_DEV_MODE=1` —— 服务端启动时授权，整进程生效；
    2. `RESEARCHPILOT_DEV_TOKEN=<令牌>` + `?dev=<令牌>` —— 服务端先配置令牌，
       再按会话临时开启，令牌用常量时间比较。

    没配令牌时 `?dev=1` 这类请求一律无效，并且记一条日志留痕。
    """
    if dev_mode_enabled():
        return True

    token = dev_token()
    if not token:
        return False

    raw = st.query_params.get(DEV_QUERY_KEY)
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    given = str(raw or "")
    if not given:
        return False
    if secrets.compare_digest(given, token):
        return True
    log.warning("开发者诊断：URL 令牌不匹配，已拒绝（这是公开地址上的越权尝试）")
    return False


def _resolve_project(projects: list[Project]) -> Project | None:
    """按优先级挑项目：URL 参数 -> 浏览器本地偏好 -> 最早创建的项目。

    最后一级刻意用最早创建的那个，而不是列表第一个：repo.list_all 按
    created_at 倒序返回，列表第一个永远是最新项目，拿它当默认值就会出现
    "刷新后跳到刚建的项目"——正是这次要修的 bug。URL 与本地偏好都失效
    （换浏览器、清缓存、项目被删）时，落到一个不随新建项目漂移的稳定值。
    """
    if not projects:
        return None
    by_id = {project.id: project for project in projects}
    for candidate in (_query_project_id(), browser_prefs.get("recent_project")):
        if isinstance(candidate, str) and candidate in by_id:
            return by_id[candidate]
    # created_at 只精确到秒，同一秒建的项目要靠 id 再排一次才稳定。
    return min(projects, key=lambda project: (str(project.created_at or ""), project.id))


def _create_project() -> None:
    """新建项目并切过去。

    必须放在 on_click 回调里：回调在控件实例化之前执行，这时才能安全地改写
    `project_picker`。直接在按钮的 if 分支里改会撞上 Streamlit 的
    "cannot be modified after the widget with that key is instantiated"。
    """
    title = str(st.session_state.get("new_project_title", ""))
    domain = str(st.session_state.get("new_project_domain", ""))
    description = str(st.session_state.get("new_project_desc", ""))
    if not title.strip():
        st.session_state["_new_project_error"] = "项目名称不能为空。"
        return

    project = repo.save(
        Project(
            id=repo.new_id("proj"),
            title=title.strip(),
            domain=domain.strip() or "地震环境噪声",
            description=description.strip(),
        )
    )
    st.session_state["project_picker"] = project.id
    st.session_state["new_project_title"] = ""
    st.session_state["_new_project_ok"] = project.title
    st.query_params[PROJECT_QUERY_KEY] = project.id
    browser_prefs.set(recent_project=project.id)
    log.info("创建项目 %s：%s", project.id, project.title)


def _rename_project() -> None:
    """改项目名称 / 领域 / 说明。

    放在 on_click 回调里：回调在控件实例化之前执行，这时才能安全地改写
    `rename_project_*` 这些控件 key（与新建项目同一个理由）。
    """
    project_id = st.session_state.get("project_picker")
    if not project_id:
        return
    project = repo.get(Project, project_id)
    if project is None:
        return

    title = str(st.session_state.get("rename_project_title", "")).strip()
    if not title:
        st.session_state["_rename_project_error"] = "项目名称不能为空。"
        return
    domain = str(st.session_state.get("rename_project_domain", "")).strip()
    description = str(st.session_state.get("rename_project_desc", "")).strip()

    changed = (
        title != project.title
        or domain != project.domain
        or description != project.description
    )
    project.title = title
    project.domain = domain or project.domain
    project.description = description
    if changed:
        repo.save(project)
        st.session_state["_rename_project_ok"] = title
        log.info("更新项目信息 %s：%s", project.id, title)


def _project_editor(project: Project) -> None:
    """侧栏：重命名当前项目。切换项目时输入框跟着换成新项目的值。"""
    if st.session_state.get("_rename_for") != project.id:
        st.session_state["rename_project_title"] = project.title
        st.session_state["rename_project_domain"] = project.domain
        st.session_state["rename_project_desc"] = project.description
        st.session_state["_rename_for"] = project.id

    with st.sidebar.popover("✎", width="stretch"):
        st.caption("编辑当前项目")
        st.text_input("项目名称", key="rename_project_title")
        st.text_input("研究领域", key="rename_project_domain")
        st.text_area("项目说明", key="rename_project_desc", height=80)
        st.button(
            "保存修改", type="primary", key="rename_project_save", on_click=_rename_project
        )
        error = st.session_state.pop("_rename_project_error", None)
        if error:
            st.error(error)
        renamed = st.session_state.pop("_rename_project_ok", None)
        if renamed:
            st.success(f"已更新：{renamed}")


def _sidebar_context(current: Project | None) -> None:
    """侧栏当前项目卡片：只保留项目身份和编辑入口。"""
    st.sidebar.markdown('<div class="rp-side-label">当前项目</div>', unsafe_allow_html=True)
    if current is None:
        st.sidebar.caption("还没有项目")
        return
    head = st.sidebar.columns([5, 2], vertical_alignment="center")
    head[0].markdown(
        f'<div class="rp-side-title">{theme.esc(current.title)}</div>',
        unsafe_allow_html=True,
    )
    with head[1].popover(
        "编辑", icon=":material/edit:", width="stretch"
    ):
        st.text_input("项目名称", key="rename_project_title")
        st.text_input("研究领域", key="rename_project_domain")
        st.text_area("项目说明", key="rename_project_desc", height=80)
        st.button(
            "保存", type="primary", key="rename_project_save", on_click=_rename_project
        )
        error = st.session_state.pop("_rename_project_error", None)
        if error:
            st.error(error)
        renamed = st.session_state.pop("_rename_project_ok", None)
        if renamed:
            st.success(f"已更新：{renamed}")
def _sidebar_new_project(projects: list[Project]) -> None:
    with st.sidebar.expander("新建项目", icon=":material/add:", expanded=not projects):
        st.text_input("项目名称", key="new_project_title")
        st.text_input("研究领域", value="地震环境噪声", key="new_project_domain")
        st.text_area("项目说明", key="new_project_desc", height=80)
        st.button("创建", type="primary", key="new_project_create", on_click=_create_project)
        error = st.session_state.pop("_new_project_error", None)
        if error:
            st.error(error)
        created = st.session_state.pop("_new_project_ok", None)
        if created:
            st.success(f"已创建：{created}")


def _sidebar_project_list(projects: list[Project], current: Project | None) -> None:
    """项目列表：搜索过滤 + 点行切换。"""
    query = str(st.session_state.get("project_search") or "").strip().lower()
    matched = [
        project for project in projects
        if not query
        or query in (project.title or "").lower()
        or query in (project.domain or "").lower()
    ]
    if not matched:
        st.caption("没有匹配的项目")
        return

    swatch_rules = []
    for project in matched:
        color = PROJECT_SWATCH_COLORS[
            int(hashlib.sha256(project.id.encode("utf-8")).hexdigest()[:8], 16)
            % len(PROJECT_SWATCH_COLORS)
        ]
        swatch_rules.append(
            f'[class*="st-key-rp_project_row_{project.id}"] '
            f'.stButton button::before{{background:{color} !important;}}'
        )
    st.sidebar.markdown(
        "<style>" + "".join(swatch_rules) + "</style>",
        unsafe_allow_html=True,
    )

    for project in matched:
        selected = current is not None and project.id == current.id
        key = "rp_project_row_selected" if selected else f"rp_project_row_{project.id}"
        with st.container(key=key):
            # 右侧 24px 的菜单按钮需要独立宽度，避免覆盖项目标题的点击区域。
            row = st.columns([5, 1], vertical_alignment="center")
            row[0].button(
                project.title or "（未命名项目）",
                key=f"project_row_{project.id}",
                on_click=_switch_project,
                args=(project.id,),
                help=project.domain or None,
                width="stretch",
            )
            with row[1]:
                with st.popover(
                    "",
                    icon=":material/more_horiz:",
                    help="项目操作",
                    width="stretch",
                    key=f"rp_project_menu_{project.id}",
                ):
                    rename_value = st.text_input(
                        "项目名称",
                        value=project.title,
                        key=f"menu_rename_value_{project.id}",
                        label_visibility="collapsed",
                    )
                    if st.button(
                        "重命名",
                        key=f"menu_rename_{project.id}",
                        width="stretch",
                    ):
                        renamed = project.model_copy(
                            update={"title": rename_value.strip()}
                        )
                        if renamed.title:
                            repo.save(renamed)
                            st.session_state["_rename_for"] = renamed.id
                            st.session_state["rename_project_title"] = renamed.title
                            st.rerun()
                    if st.button(
                        "在新项目中继续",
                        key=f"project_continue_{project.id}",
                        width="stretch",
                    ):
                        clone = Project(
                            id=repo.new_id("proj"),
                            title=f"{project.title}（继续）",
                            domain=project.domain,
                            description=project.description,
                        )
                        repo.save(clone)
                        for paper in repo.list_all(Paper, project.id):
                            library.attach_to_project(clone.id, paper)
                        st.session_state["project_picker"] = clone.id
                        st.query_params[PROJECT_QUERY_KEY] = clone.id
                        st.session_state.pop("selected_paper_id", None)
                        st.rerun()
                    confirm_delete = st.checkbox(
                        "确认删除项目",
                        key=f"project_delete_confirm_{project.id}",
                    )
                    if st.button(
                        "删除",
                        key=f"project_delete_{project.id}",
                        disabled=not confirm_delete,
                        width="stretch",
                    ):
                        for paper in repo.list_all(Paper, project.id):
                            library.remove_paper(paper, delete_file=False)
                        repo.delete(Project, project.id)
                        st.session_state.pop("project_picker", None)
                        st.session_state.pop("selected_paper_id", None)
                        st.session_state.pop("_rename_for", None)
                        st.rerun()
def _sidebar_footer(identity: dict, parse_ok: bool, schema: dict) -> None:
    """侧栏底部：AI 模型与 API。状态只有这一处，别处不再重复报。"""
    connected = llm_settings.connected()
    label = "AI 模型 · " + ("已连接" if connected else "未连接")
    with st.sidebar.container(key="rp_sidebar_footer"):
        with st.container(key="rp_ai_connected" if connected else "rp_ai_disconnected"):
            with st.popover(label, width="stretch"):
                llm_settings.render()
        if not parse_ok:
            st.sidebar.error("PDF 解析组件缺失，上传文献会失败。")
        if st.session_state.get("_dev_mode"):
            with st.popover("开发者诊断", icon=":material/monitor_heart:", width="stretch"):
                st.caption("仅开发者模式可见。开启方式见 docs/00_项目启动/界面规范_v1.0.md。")
                st.write("数据库：", identity["path"])
                st.write("启动时已存在：", identity["existed_at_startup"])
                st.write("身份标签：", identity["label"] or "（无）")
                st.write(
                    "记录数：项目", len(repo.list_all(Project)),
                    "／文献", len(repo.list_all(Paper)),
                )
                st.write("模型：", LLM.describe())
                st.write("版本：", APP_VERSION)
                st.write("schema 版本：", schema["schema_version"])
                st.write("原始数据（只读）：", str(PATHS.raw_sac_dir))
                st.write("原始数据目录存在：", PATHS.raw_sac_dir.is_dir())


def _sidebar(projects: list[Project], identity: dict) -> Project | None:
    """渲染整个左侧导航，返回当前项目。

    顺序：产品名 → 搜索项目 → 当前项目 → 新建项目 → 所有项目 → AI 模型。
    """
    resolved = _resolve_project(projects) if projects else None
    if projects:
        ids = [project.id for project in projects]
        # 会话里存的项目可能已被删掉，或属于别的地址栏参数：先纠偏再建控件。
        if st.session_state.get("project_picker") not in ids:
            st.session_state["project_picker"] = resolved.id
        if _query_project_id() != resolved.id:
            st.query_params[PROJECT_QUERY_KEY] = resolved.id
    current = repo.get(Project, st.session_state.get("project_picker")) if projects else None
    if current is not None and st.session_state.get("_rename_for") != current.id:
        st.session_state["rename_project_title"] = current.title
        st.session_state["rename_project_domain"] = current.domain
        st.session_state["rename_project_desc"] = current.description
        st.session_state["_rename_for"] = current.id

    if identity["label"]:
        st.sidebar.warning(f"当前数据库：{identity['label']}")
    if not identity["existed_at_startup"]:
        st.sidebar.error(
            "本次启动新建了一个空数据库。如果你本应看到已有项目，"
            "先确认启动目录或 RESEARCHPILOT_DB 指向是否正确。"
        )

    st.sidebar.markdown('<div class="rp-brand">ResearchPilot</div>', unsafe_allow_html=True)
    st.sidebar.text_input(
        "搜索项目", key="project_search", placeholder="搜索项目",
        label_visibility="collapsed", icon=":material/search:",
    )
    _sidebar_context(current)
    _sidebar_new_project(projects)

    st.sidebar.markdown(
        f'<div class="rp-side-label">所有项目（{len(projects)}）</div>',
        unsafe_allow_html=True,
    )
    with st.sidebar.container(key="rp_project_list_all"):
        _sidebar_project_list(projects, current)

    return current


def main() -> None:
    PATHS.ensure()
    st.session_state["_dev_mode"] = _dev_mode()
    # 数据库身份：启动时记一次。新建空库必须显式提示，不能静默换库
    identity = db_identity()
    schema = _ensure_schema()
    # 只在真的影响用户可用性时才提示；环境细节一律进诊断模式
    parse_ok, parse_detail = pymupdf_status()
    projects = repo.list_all(Project)
    # 文献角标链接是 Streamlit 的临时查询态；首轮先把目标交给浏览器偏好组件，
    # 后续重跑再据此恢复同一篇论文与页码。
    from_citation = bool(_query_param(CITE_QUERY_KEY))
    if not from_citation:
        browser_prefs.load()
    _consume_cite_claim(projects)
    if from_citation:
        browser_prefs.load()
    project = _sidebar(projects, identity)
    _sidebar_footer(identity, parse_ok, schema)

    if project is None:
        theme.page_title(APP_NAME, f"v{APP_VERSION}")
        st.caption("暂无项目")
        assistant.render(None, screen=None)
        return

    navigation = st.session_state.get("_graph_navigation")
    if (
        isinstance(navigation, dict)
        and navigation.get("project_id") == project.id
        and navigation.get("target") in {"outcomes", "planner"}
    ):
        target = str(navigation.get("target"))
        st.button(
            "← 返回引用图谱",
            key="graph_navigation_back",
            type="tertiary",
            on_click=_return_from_graph_navigation,
        )
        if target == "outcomes":
            section = str(navigation.get("section") or "method_outline")
            st.session_state[f"report_section_{project.id}"] = section
            st.query_params[VIEW_QUERY_KEY] = "outcomes"
            browser_prefs.set(recent_view="outcomes")
            outcomes_page.render(project)
            assistant.render(project, screen="科研成果")
        else:
            draft_id = str(navigation.get("draft_id") or "")
            if draft_id:
                st.session_state["_graph_open_draft"] = draft_id
            st.query_params[VIEW_QUERY_KEY] = "planner"
            browser_prefs.set(recent_view="planner")
            planner_page.render(project)
            assistant.render(project, screen="复现规划")
        return

    # 普通打开仍统一落到文献阅读；只有图谱导出后的显式跳转会进入
    # 成果/规划页，旧 bookmarked planner / outcomes 链接保持原回退行为。
    if _query_param(VIEW_QUERY_KEY) != LITERATURE_VIEW_CODE:
        st.query_params[VIEW_QUERY_KEY] = LITERATURE_VIEW_CODE
    browser_prefs.set(recent_view=LITERATURE_VIEW_CODE)

    # 左侧原生侧栏是固定导航；中间工作区使用全视口独立滚动容器。
    with st.container(key="rp_workspace_shell"):
        literature_page.render(project)
    # 全局悬浮助手：只围绕当前项目和文献阅读回答。
    assistant.render(project, screen=LITERATURE_VIEW_LABEL)


main()
