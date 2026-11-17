"""统一视觉规范：全项目唯一一处 CSS 与设计 token（方案 C 混合）。

风格：**A 期刊风的浅色阅读底座**（近白纸面、深青强调、引文衬线体、小圆角）
      ＋ **B 的导航与状态组织**（紧凑索引、状态条、可点选的步骤行）。

三条原则（沿用 v1.0）：

1. **只用官方钩子。** Streamlit 给带 `key` 的容器生成 `.st-key-<key>` 类名，
   这是官方支持的定制入口；不写大范围选择器，也不依赖内部类名与 `--st-*` 变量。
2. **颜色用中性灰半透明叠加 + 语义色 token**，浅色/深色共用一套值。
3. **字号与语义色交回 Streamlit**；标题、警告、错误继续用原生元素，保证无障碍与缩放。

对比度由 `scripts/ui_browser_check.py`（Playwright + Edge）在真实渲染中校验：
正文字号 ≥4.5:1、状态徽标 ≥4.5:1。token 值是按实测调过的，改动后必须重跑该脚本。

`chip()/field_rows()/empty_state()/status_strip()` 这些 helper 会转义传入文本：
它们最终以 HTML 渲染，未转义的用户数据会变成注入点。
"""

from __future__ import annotations

import html as _html

import streamlit as st

# 间距节奏：索引行、区块之间统一用这两档，避免各处随手写数字
ROW_PADDING = "0.28rem 0.5rem"
BLOCK_GAP = "0.6rem"

# 索引栏宽度（像素）。下限保证控件不至于压扁到不可用，上限避免主内容被挤没。
INDEX_WIDTH_MIN = 240
INDEX_WIDTH_MAX = 720
INDEX_WIDTH_DEFAULT = 380

# 设计 token（供 Python 侧与测试引用；CSS 里同名出现一次）
TOKENS: dict[str, str] = {
    "space_1": "4px", "space_2": "8px", "space_3": "12px", "space_4": "16px",
    "space_5": "24px", "space_6": "32px",
    "text_xs": "12px", "text_sm": "13px", "text_base": "15px", "text_lg": "17px",
    "text_xl": "20px",
    "radius_sm": "4px", "radius_md": "6px", "radius_lg": "8px", "radius_pill": "999px",
    "bg_page": "#FFFFFF", "bg_nav": "#FAFAFA", "bg_rail": "#FAFAFA",
    # 三级文字：正文 / 次级（captions、字段标签）/ 三级（纯元数据，如年份、DOI）
    # fg-3 是"纯元数据"档：在浅色侧栏（#F6F7F5）上实测 4.49:1，差一点点不够 AA，
    # 所以再压深一档（audit_layout.py 复核 ≥4.5:1）。
    "fg_1": "#333333", "fg_2": "#666666", "fg_3": "#888888",
    "primary": "#647A70", "info": "#5B7FA6", "success": "#5F876C",
    "warning": "#A7794A", "danger": "#B25C56", "evidence": "#786A9E",
    "control_h": "32px", "row_h": "30px",
}

_CSS = """
<style>
  /* ---------- Design tokens ---------- */
  :root{
    --rp-space-1:4px; --rp-space-2:8px; --rp-space-3:12px; --rp-space-4:16px;
    --rp-space-5:24px; --rp-space-6:32px;
    --rp-text-xs:12px; --rp-text-sm:13px; --rp-text-base:15px; --rp-text-lg:17px;
    --rp-text-xl:20px;
    --rp-radius-sm:6px; --rp-radius-md:8px; --rp-radius-lg:12px; --rp-radius-pill:999px;
    --rp-border:rgba(23,26,28,.10); --rp-border-strong:rgba(23,26,28,.20);
    --rp-bg-page:#FFFFFF; --rp-bg-nav:#F6F7F5; --rp-bg-rail:#F7F8F6; --rp-bg-card:#FFFFFF;
    --rp-fg-1:#16181A; --rp-fg-2:#3F474E; --rp-fg-3:#626A71;
    --rp-primary:#1F6E6A; --rp-primary-soft:rgba(31,111,107,.12);
    --rp-info:#285FCA; --rp-success:#2A7242; --rp-warning:#8A5200;
    --rp-danger:#B3261E; --rp-evidence:#6B4FA8;
    --rp-control-h:32px; --rp-row-h:30px;
    --rp-font-serif:Georgia,"Times New Roman","Noto Serif SC","Songti SC",serif;
    --rp-font-mono:ui-monospace,"JetBrains Mono","SFMono-Regular",Consolas,monospace;
  }

  /* ---------- 全局文字层级 ----------
     两级就够：正文用 fg-1；caption / 字段标签统一走"中等灰度" fg-2，
     只有年份、DOI 这类纯元数据才用 fg-3。Streamlit 的原生 caption 偏浅，
     在一屏塞满小字时主次会糊掉，所以这里统一提一档。
     用 data-testid 而不是内部类名：它是官方测试钩子，比 CSS module 类名稳定。 */
  section[data-testid="stMain"] [data-testid="stCaptionContainer"] p,
  section[data-testid="stMain"] [data-testid="stCaptionContainer"] {
      color: var(--rp-fg-2);
  }
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
      color: var(--rp-fg-3);
  }
  /* 纯元数据（年份、DOI、字数…）：更小更浅，明确不是正文 */
  .rp-meta, .rp-meta p{
      font-size: var(--rp-text-xs) !important; color: var(--rp-fg-3) !important;
  }

  /* ---------- 控件尺寸统一 ---------- */
  section[data-testid="stMain"] .stButton > button,
  section[data-testid="stSidebar"] .stButton > button {
      min-height: var(--rp-control-h);
  }

  /* ---------- 侧栏：品牌 / 上下文 / 导航 / 项目列表 ---------- */
  section[data-testid="stSidebar"]{
      width: 260px !important; min-width: 260px !important; max-width: 260px !important;
      height: 100vh !important;
      background: var(--rp-bg-nav);
      border-right: 1px solid var(--rp-border);
      overflow-y: auto;
  }
  [data-testid="stSidebarCollapseButton"],
  [data-testid="collapsedControl"],
  section[data-testid="stSidebar"] [data-testid="stSidebarHeader"]{
      display: none !important;
  }
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"]{
      padding-top: 0.6rem;
      padding-bottom: 0.6rem;
  }
  section[data-testid="stSidebar"] [data-testid="stSidebarContent"]{
      height: 100% !important;
  }
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"]{
      min-height: 100%;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
  }
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div{
      min-height: 0;
      flex: 1 1 auto;
      display: flex;
      flex-direction: column;
  }
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div
      > [data-testid="stVerticalBlock"]{
      min-height: 0;
      flex: 1 1 auto;
      display: flex;
      flex-direction: column;
  }
  .rp-brand{
      font-size: var(--rp-text-base); font-weight: 650; letter-spacing: .01em;
      color: var(--rp-fg-1); line-height: 1.2;
  }
  .rp-side-title{
      font-size: var(--rp-text-base); font-weight: 600; color: var(--rp-fg-1);
      line-height: 1.3; overflow-wrap: anywhere;
  }
  .rp-side-label{
      font-size: var(--rp-text-xs); font-weight: 600; letter-spacing: .04em;
      color: var(--rp-fg-3); margin: var(--rp-space-3) 0 var(--rp-space-1);
  }
  /* 工作台导航：radio 承载交互，外观是紧凑列表行 */
  .st-key-rp_nav_workbench [data-testid="stRadio"] > div[role="radiogroup"]{
      gap: 2px;
  }
  .st-key-rp_nav_workbench [data-testid="stWidgetLabel"]{
      display: none !important;
  }
  .st-key-rp_nav_workbench label{
      display: flex; align-items: center; min-height: var(--rp-row-h);
      padding: 0 10px; border-radius: var(--rp-radius-sm);
      font-size: var(--rp-text-sm); color: var(--rp-fg-1);
      border-left: 2px solid transparent;
  }
  .st-key-rp_nav_workbench label::before{
      font-family: "Material Symbols Rounded";
      font-size: 18px; line-height: 1; width: 18px; margin-right: 8px;
      color: var(--rp-fg-2); font-weight: 400;
  }
  .st-key-rp_nav_workbench label:nth-of-type(1)::before{ content: "article"; }
  .st-key-rp_nav_workbench label:nth-of-type(2)::before{ content: "account_tree"; }
  .st-key-rp_nav_workbench label:nth-of-type(3)::before{ content: "description"; }
  .st-key-rp_nav_workbench label:hover{ background: rgba(23,26,28,.05); }
  .st-key-rp_nav_workbench label:has(input:checked){
      background: var(--rp-primary-soft); border-left-color: var(--rp-primary);
      font-weight: 600;
  }
  .st-key-rp_nav_workbench label:has(input:checked)::before{
      color: var(--rp-primary); font-variation-settings: "FILL" 1;
  }
  .st-key-rp_nav_workbench [data-baseweb="radio"] > div:first-child{ display: none !important; }
  /* 项目行。key 是 rp_project_row_<id> 与 rp_project_row_selected，
     所以按前缀匹配，选中态再用第二条规则覆盖。 */
  [class*="st-key-rp_project_row"] .stButton button{
      justify-content: flex-start; text-align: left; width: 100%;
      min-height: var(--rp-row-h); padding: 0 10px; border: none; background: transparent;
      font-size: var(--rp-text-sm); font-weight: 400; color: var(--rp-fg-1);
  }
  [class*="st-key-rp_project_row"] .stButton button p,
  [class*="st-key-rp_project_row"] .stButton button div{
      justify-content: flex-start !important; text-align: left !important;
      width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  [class*="st-key-rp_project_row"] .stButton button{
      justify-content: flex-start !important; text-align: left !important;
      width: 100%;
  }
  [class*="st-key-rp_project_row"] .stButton button p,
  [class*="st-key-rp_project_row"] .stButton button div{
      justify-content: flex-start !important; text-align: left !important;
      width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  [class*="st-key-rp_project_row"] [data-testid="stHorizontalBlock"]{
    gap:2px; align-items:center;
  }
  [class*="st-key-rp_project_menu_"] [data-testid="stPopover"] button{
    opacity:.2; min-width:24px; width:24px; padding:0; color:var(--rp-fg-3);
  }
  [class*="st-key-rp_project_row"]:hover [class*="st-key-rp_project_menu_"] [data-testid="stPopover"] button,
  [class*="st-key-rp_project_menu_"] [data-testid="stPopover"] button:focus-visible{
    opacity:1;
  }
  [class*="st-key-rp_project_row"] .stButton button p,
  [class*="st-key-rp_project_row"] .stButton button div{
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  [class*="st-key-project_delete_"] button{ color:var(--rp-danger) !important; }
  [class*="st-key-rp_project_row"] .stButton button:hover{ background: rgba(23,26,28,.05); }
  .st-key-rp_project_row_selected .stButton button{
      background: var(--rp-primary-soft); font-weight: 600;
      border-left: 2px solid var(--rp-primary); border-radius: var(--rp-radius-sm);
  }
  /* AI 状态点：用伪元素画，不用字形图标 */
  /* 文献库：列表行，不用卡片边框 */
  [class*="st-key-rp_library_row"]{
      border-bottom: 1px solid var(--rp-border);
      padding: var(--rp-space-2) 0 var(--rp-space-1);
  }
  [class*="st-key-rp_library_row_selected"]{
      background: rgba(31,110,106,.07);
      border-left: 2px solid var(--rp-primary);
      padding-left: var(--rp-space-2);
  }
  [class*="st-key-rp_library_row"]:last-child{ border-bottom: none; }
  [class*="st-key-rp_library_row"] p{ margin-bottom: 2px; }
  /* 批量整理工作条：选择、分类和危险操作集中在一个稳定区域。 */
  .st-key-rp_library_batch_workspace{
      margin: 4px 0 var(--rp-space-2);
      padding: var(--rp-space-3);
      background: #F8FAF9;
      border: 1px solid rgba(31,110,106,.20);
      border-radius: var(--rp-radius-md);
  }
  .st-key-rp_library_batch_workspace [data-testid="stHorizontalBlock"]{
      gap: var(--rp-space-2);
  }
  .st-key-rp_library_batch_workspace [data-testid="stCaptionContainer"]{
      margin-bottom: 2px;
  }
  .st-key-rp_library_batch_workspace [data-testid="stCaptionContainer"] p{
      font-size: var(--rp-text-xs);
  }
  /* 论文大纲：章节行 + 当前章节高亮（左侧竖条 + 底色） */
  [class*="st-key-rp_outline_row"] .stButton > button,
  .st-key-rp_outline_active .stButton > button{
      justify-content: flex-start; text-align: left; width: 100%;
      min-height: var(--rp-row-h); padding: 2px 8px; border: none; background: transparent;
      font-size: var(--rp-text-sm); font-weight: 400; color: var(--rp-fg-2);
      border-left: 2px solid transparent; border-radius: var(--rp-radius-sm);
  }
  [class*="st-key-rp_outline_row"] .stButton > button:hover{ background: rgba(23,26,28,.05); }
  .st-key-rp_outline_active .stButton > button{
      background: var(--rp-primary-soft); color: var(--rp-fg-1); font-weight: 600;
      border-left-color: var(--rp-primary);
  }

  .st-key-rp_ai_disconnected [data-testid="stPopover"] button::before,
  .st-key-rp_ai_connected [data-testid="stPopover"] button::before{
      content: ""; width: 7px; height: 7px; border-radius: 50%;
      display: inline-block; margin-right: 8px; flex: 0 0 auto;
  }
  .st-key-rp_ai_disconnected [data-testid="stPopover"] button::before{ background: var(--rp-danger); }
  .st-key-rp_ai_connected [data-testid="stPopover"] button::before{ background: var(--rp-success); }

  /* 数字列对齐：指标、页码、时长、计数 */
  .rp-num, [data-testid="stMetricValue"]{
      font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1;
  }
  /* 键盘可达：焦点必须看得见（不用 outline:none 把默认焦点抹掉） */
  .st-key-rp_index button:focus-visible,
  [class*="st-key-rp_project_row"] button:focus-visible,
  .st-key-rp_nav_workbench label:focus-within{
      outline: 2px solid var(--rp-primary); outline-offset: 2px; border-radius: var(--rp-radius-sm);
  }
  /* 触摸与动效：双击缩放延迟、以及尊重"减少动态效果" */
  button, [role="button"], label[data-baseweb="radio"]{ touch-action: manipulation; }
  @media (prefers-reduced-motion: reduce){
      *{ transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
  }
  /* 侧栏页脚（AI 模型与 API）：贴在滚动区底部 */
  .st-key-rp_sidebar_footer{
      position: sticky; bottom: 0; z-index: 5;
      margin-top: auto !important;
      flex: 0 0 auto !important;
      background: var(--rp-bg-nav); border-top: 1px solid var(--rp-border);
      padding: var(--rp-space-2) 0 var(--rp-space-3);
  }
  section[data-testid="stSidebar"]
      [data-testid="stLayoutWrapper"]:has(.st-key-rp_sidebar_footer){
      margin-top: auto !important;
      flex: 0 0 auto !important;
  }
  .st-key-rp_sidebar_footer .stButton > button,
  .st-key-rp_sidebar_footer [data-testid="stPopover"] button{
      justify-content: flex-start; text-align: left; width: 100%;
      min-height: var(--rp-control-h); border: 1px solid var(--rp-border);
      background: var(--rp-bg-card); font-size: var(--rp-text-sm); font-weight: 500;
      white-space: nowrap !important; flex-wrap: nowrap !important;
  }
  .st-key-rp_sidebar_footer [data-testid="stPopover"] button p{
      white-space: nowrap !important; overflow: hidden; text-overflow: ellipsis;
  }
  .rp-status-dot{
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      margin-right: 6px; background: var(--rp-fg-3); vertical-align: middle;
  }
  .rp-status-dot--on{ background: var(--rp-success); }
  .rp-status-dot--off{ background: var(--rp-danger); }
  /* 侧栏里的原生告警（数据库身份这类安全提示）压缩高度并提高对比度 */
  section[data-testid="stSidebar"] [data-testid="stAlert"]{
      padding: 6px 10px; border-radius: var(--rp-radius-sm);
  }
  section[data-testid="stSidebar"] [data-testid="stAlert"] p{
      font-size: var(--rp-text-xs); line-height: 1.45; color: #4F2E00;
  }

  /* ---------- 左栏索引（工作台共用） ---------- */
  .st-key-rp_index {
      background: var(--rp-bg-rail);
      border: 1px solid var(--rp-border);
      border-radius: var(--rp-radius-md);
      padding: 0.55rem 0.6rem 0.15rem 0.6rem;
  }
  .st-key-rp_index .stButton > button {
      justify-content: flex-start; text-align: left;
      padding: 0.28rem 0.5rem; min-height: 0; line-height: 1.35;
      border: none; background: transparent; font-weight: 400;
  }
  .st-key-rp_index .stButton > button:hover { background: rgba(127,127,127,.12); }
  .st-key-rp_index_row_selected {
      background: rgba(127,127,127,.16);
      border-left: 3px solid var(--rp-primary);
      border-radius: 4px;
  }
  .st-key-rp_detail { padding-top: 0.1rem; }

  /* ---------- 阅读区排版 ---------- */
  .rp-lede{ font-size: var(--rp-text-sm); color: var(--rp-fg-3); margin: 0 0 var(--rp-space-4); }
  .rp-section-title{
      font-size: var(--rp-text-sm); font-weight: 650; color: var(--rp-fg-2);
      margin: var(--rp-space-5) 0 var(--rp-space-2); display: flex; gap: var(--rp-space-2);
      align-items: baseline;
  }
  .rp-section-title .rp-note{ font-weight: 400; font-size: var(--rp-text-xs); color: var(--rp-fg-3); }
  /* 引文：衬线 + 左侧细线，与正文区分 */
  .rp-quote{
      font-family: var(--rp-font-serif); font-size: var(--rp-text-sm); color: var(--rp-fg-2);
      line-height: 1.8; border-left: 2px solid var(--rp-border-strong);
      padding-left: var(--rp-space-3); margin: var(--rp-space-2) 0;
  }
  /* 字段行：替代零散 caption，一屏两级文字 */
  .rp-fields{ margin: var(--rp-space-2) 0; }
  .rp-field{
      display: grid; grid-template-columns: 88px 1fr; gap: var(--rp-space-3);
      padding: var(--rp-space-2) 0; border-bottom: 1px dashed var(--rp-border);
  }
  .rp-field:last-child{ border-bottom: none; }
  .rp-field__k{ font-size: var(--rp-text-xs); color: var(--rp-fg-3); padding-top: 2px; }
  .rp-field__v{ font-size: var(--rp-text-sm); color: var(--rp-fg-1); }

  /* ---------- 状态徽标 ---------- */
  .rp-chip{
      display: inline-flex; align-items: center; gap: 5px;
      height: 22px; padding: 0 8px; border-radius: var(--rp-radius-pill);
      font-size: var(--rp-text-xs); border: 1px solid var(--rp-border);
      background: var(--rp-bg-card); color: var(--rp-fg-2); white-space: nowrap;
  }
  .rp-chip--evidence{
      color: var(--rp-evidence); border-color: rgba(107,79,168,.35);
      background: rgba(107,79,168,.07); font-family: var(--rp-font-mono);
  }
  .rp-chip--link{ cursor: pointer; }
  .rp-chip--ok{ color: var(--rp-success); border-color: rgba(42,114,66,.30); background: rgba(42,114,66,.08); }
  .rp-chip--warn{ color: var(--rp-warning); border-color: rgba(138,82,0,.30); background: rgba(138,82,0,.08); }
  .rp-chip--danger{ color: var(--rp-danger); border-color: rgba(179,38,30,.30); background: rgba(179,38,30,.08); }
  .rp-chip--ai{ color: var(--rp-info); border-color: rgba(40,95,202,.30); background: rgba(40,95,202,.08); }
  .rp-chip-row{ display: flex; gap: var(--rp-space-2); flex-wrap: wrap; margin: var(--rp-space-2) 0; }

  /* ---------- 状态条 / 进度 ---------- */
  .rp-statusline{ display: flex; align-items: center; gap: var(--rp-space-3); margin: var(--rp-space-3) 0; }
  .rp-progress{ flex: 1; height: 6px; border-radius: 3px; background: var(--rp-border); overflow: hidden; }
  .rp-progress > i{ display: block; height: 100%; background: var(--rp-primary); width: 0; }
  .rp-stages{ display: flex; gap: 6px; margin: var(--rp-space-2) 0; }
  .rp-stage{ flex: 1; height: 6px; border-radius: 3px; background: var(--rp-border); }
  .rp-stage--done{ background: var(--rp-success); }
  .rp-stage--run{ background: var(--rp-info); }

  /* ---------- 双栏独立滚动容器 ----------
     两个坑，都在这里一次性解决：
     1) 分栏是一行 flex，子项默认被 stretch 拉成与最高子项等高，
        那样"给滚动容器设高度"会被忽略，整页仍被撑高 → 先让分栏子项按自身高度排；
     2) Streamlit 自己的 `height: auto` 与同等特异度的类选择器会覆盖我们的高度 →
        对我方这两个具名容器（官方 key 钩子）显式加 !important，只覆盖它们自身。 */
  .st-key-rp_index, .st-key-rp_detail { align-self: flex-start !important; }
  .st-key-rp_pdf_scroll, .st-key-rp_content_scroll {
      height: var(--rp-read-h) !important;
      max-height: var(--rp-read-h) !important;
      min-height: 320px;
      overflow-y: auto !important;
      overflow-x: hidden !important;
      padding-right: 6px;
      align-self: flex-start !important;
  }

  /* ---------- 阅读页：固定两行 + 下方双栏 ----------
     阅读区高度按视口算：--rp-read-offset 是"顶部两行 + 应用外边距"的实测高度，
     改这里之前先量一遍（scripts/verify_new_ui.py 会断言页面不出现整页滚动）。 */
  :root{
      --rp-topbar-h: 0px;
      --rp-read-offset: calc(var(--rp-topbar-h) + 250px);
      --rp-read-h: calc(100vh - var(--rp-read-offset));
  }
  @media (max-height: 820px){ :root{ --rp-read-offset: calc(var(--rp-topbar-h) + 180px); } }
  /* ---------- 全站顶部导航（固定不随正文滚动） ---------- */
  .st-key-rp_topbar{
      /* fixed 而不是 sticky：Streamlit 的 flex 包装层会让 sticky 失效（实测过）。
         左边界由 topbar 里的小组件量出来写到变量上，侧栏开合时自动跟随。
         量出来之前先隐藏：否则它会在 width 兜底值下盖住侧栏，把侧栏点击全吃掉。 */
      visibility: hidden;
      position: fixed; top: 0; left: var(--rp-topbar-left, 0px);
      width: var(--rp-topbar-width, 100%); right: auto; z-index: 60;
      background: var(--rp-bg-page);
      padding: var(--rp-space-2) var(--rp-space-4) var(--rp-space-2);
      border-bottom: 1px solid var(--rp-border);
  }
  html[data-rp-topbar-ready="1"] .st-key-rp_topbar{ visibility: visible; }
  /* 拖拽分隔条的高度必须跟着阅读区走：它原本有个固定的 620px，
     会把整行撑到 626px，阅读页于是永远比视口高出 100px 左右（实测）。 */
  [class*="st-key-rp_split_"]{ height: var(--rp-read-h, 620px) !important; }
  [class*="st-key-rp_split_"] > div,
  [class*="st-key-rp_split_"] > div > div,
  [class*="st-key-rp_split_"] [data-testid="stBidiComponentIsolated"]{
      height: var(--rp-read-h, 620px) !important;
  }
  .rp-topbar-spacer{ height: var(--rp-topbar-h); }
  .st-key-rp_topbar [data-testid="stVerticalBlock"]{ gap: var(--rp-space-1); }
  /* Streamlit 自带的顶部 header 是 60px 高、z-index 999990 的绝对定位层，
     它会把顶栏上的点击全吃掉（实测报"intercepts pointer events"）。
     不能直接 display:none —— 侧栏的"展开"按钮就在这一层里，隐藏后侧栏收起
     就再也打不开了（浏览器验收抓到过）。所以只关掉它自己那一层的指针事件，
     再把它内部的真实控件放回来。 */
  header[data-testid="stHeader"]{ display: none !important; }
  .rp-topbar__title{
      font-size: var(--rp-text-base); font-weight: 650; color: var(--rp-fg-1);
      line-height: 1.25; overflow-wrap: anywhere;
  }
  .rp-topbar__sub{ font-size: var(--rp-text-xs); color: var(--rp-fg-3); margin-top: 1px; }
  .st-key-rp_topbar .stButton > button{
      min-height: 28px; padding: 0 8px; font-size: var(--rp-text-sm);
      border-radius: var(--rp-radius-sm);
  }
  .st-key-rp_topbar .stButton > button:disabled{
      opacity: 1; background: var(--rp-primary-soft); color: var(--rp-fg-1);
      border-color: transparent; font-weight: 650;
  }

  /* ---------- 三栏全视口壳层 ---------- */
  html, body, .stApp{
      height: 100vh !important; overflow: hidden !important;
  }
  section[data-testid="stMain"]{
      height: 100vh !important; overflow: hidden !important;
  }
  [data-testid="stMainBlockContainer"]{
      height: 100vh !important; max-height: 100vh !important;
      padding: 0 !important; overflow: hidden !important; max-width: none !important;
  }
  .st-key-rp_workspace_shell{
      height: 100vh !important; overflow-y: auto !important;
      min-height: 100vh !important; flex-shrink: 0 !important;
      padding: 8px 14px 10px 14px;
      margin-top: -32px !important;
      box-sizing: border-box;
  }
  [data-testid="stMainBlockContainer"] > div:has(.st-key-rp_workspace_shell){
      height: 100vh !important; min-height: 100vh !important;
  }
  .st-key-rp_workspace_shell .stHorizontalBlock:has(.st-key-rp_index){
      height: calc(100vh - 16px) !important;
      min-height: calc(100vh - 16px) !important;
      align-items: stretch !important;
  }
  .st-key-rp_workspace_shell .st-key-rp_detail{
      height: calc(100vh - 16px);
      max-height: calc(100vh - 16px);
      overflow-y: auto !important;
      padding-right: 4px;
      flex-shrink: 0 !important;
  }
  .st-key-rp_workspace_shell .st-key-rp_index{
      height: calc(100vh - 16px);
      max-height: calc(100vh - 16px);
      overflow-y: auto !important;
      flex-shrink: 0 !important;
  }
  .st-key-rp_main_toolbar{
      position: sticky; top: 0; z-index: 8;
      background: var(--rp-bg-page);
      border-bottom: 1px solid var(--rp-border);
      padding: 2px 0 8px 0; margin-bottom: 8px;
  }
  /* 豆包式文献摘要卡片：标题+元数据直接置顶，操作收进右上角 ⋯ */
  .st-key-rp_paper_card{
      background: var(--rp-bg-card);
      border: 1px solid var(--rp-border);
      border-radius: var(--rp-radius-lg);
      padding: var(--rp-space-4) var(--rp-space-5) var(--rp-space-3);
      margin-bottom: var(--rp-space-3);
      box-shadow: 0 1px 3px rgba(23,26,28,.04);
  }
  .st-key-rp_paper_card [data-testid="stHorizontalBlock"]{ gap: var(--rp-space-2); }
  .rp-paper-card__title{
      font-size: var(--rp-text-lg); font-weight: 680; color: var(--rp-fg-1);
      line-height: 1.35; overflow-wrap: anywhere;
  }
  .rp-paper-card__meta{
      margin-top: var(--rp-space-2);
      font-size: var(--rp-text-xs); color: var(--rp-fg-3);
      line-height: 1.5; overflow-wrap: anywhere;
  }
  [class*="st-key-rp_relation_card_"]{
      border-bottom: 1px solid var(--rp-border);
      padding: 12px 2px 10px;
  }
  [class*="st-key-rp_relation_card_"]:last-child{ border-bottom: 0; }
  [class*="st-key-rp_relation_card_"] [data-testid="stHorizontalBlock"]{
      align-items: center;
  }
  [class*="st-key-rp_relation_card_"] [class*="st-key-open_relation_"] button{
      justify-content: flex-start; text-align: left;
      padding: 0; border: 0; background: transparent;
      min-height: 0; height: auto;
  }
  [class*="st-key-rp_relation_card_"] [class*="st-key-open_relation_"] button p{
      font-size: var(--rp-text-base); font-weight: 600;
      line-height: 1.4; color: var(--rp-fg-1);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      text-align: left;
  }
  [class*="st-key-rp_relation_card_"] [class*="st-key-open_relation_"] button:hover{
      background: transparent;
  }
  [class*="st-key-rp_relation_card_"] [class*="st-key-open_relation_"] button:hover p{
      color: var(--rp-primary);
  }
  .rp-rel-year{
      font-size: var(--rp-text-xs); color: var(--rp-fg-3);
      white-space: nowrap; line-height: 1.4;
  }
  .rp-rel-description{
      margin: 4px 0 8px; color: var(--rp-fg-2);
      font-size: var(--rp-text-sm); line-height: 1.65;
  }
  .rp-graph-group{
      display:inline-block; margin-right:6px; padding:0 5px;
      border-radius:3px; background:#F0F4F2; color:#53645C;
      font-size:10px; line-height:1.6; vertical-align:1px;
  }
  .rp-rel-verify{
      display: inline-block; color: var(--rp-fg-3);
      font-size: 11px; line-height: 1.4; white-space: nowrap;
  }
  .rp-rel-verified{
      display: inline-block; color:#426A55; background:#F0F5F1;
      padding:1px 6px; border-radius:3px; font-size:10px; line-height:1.5;
      white-space:nowrap;
  }
  .rp-relation-value{
      margin:8px 0 12px; padding:10px 12px;
      border:1px solid rgba(31,110,106,.18); border-radius:6px;
      background:#F7FAF8; color:var(--rp-fg-2);
      font-size:var(--rp-text-sm); line-height:1.65;
  }
  .rp-relation-value b{ color:var(--rp-fg-1); font-weight:600; }
  .rp-relation-selected-marker,
  .rp-relation-focus-marker{
      display:none;
  }
  [class*="st-key-rp_relation_card_"]:has(.rp-relation-focus-marker){
      border-left:3px solid var(--rp-primary);
      background:rgba(31,110,106,.035);
      padding-left:calc(var(--rp-space-3) - 3px);
  }
  [class*="st-key-rp_relation_card_"]:has(.rp-relation-selected-marker){
      border-radius:var(--rp-radius-sm);
      box-shadow:0 0 0 2px rgba(40,95,202,.26);
      background:rgba(40,95,202,.045);
  }
  [class*="st-key-rp_relation_card_"] [data-testid="stExpander"]{
      margin: 2px 0 4px;
  }
  [class*="st-key-rp_relation_card_"] [data-testid="stExpander"] summary p{
      font-size: var(--rp-text-xs); color: var(--rp-fg-3);
  }
  .rp-overview-copy{
      margin:6px 0 18px; font-size:var(--rp-text-sm); line-height:1.75;
      color:var(--rp-fg-1); overflow-wrap:anywhere;
  }
  .rp-overview-list{
      margin:6px 0 18px; padding-left:1.25em; color:var(--rp-fg-1);
  }
  .rp-overview-item{
      margin:5px 0; padding-left:2px; font-size:var(--rp-text-sm);
      line-height:1.75; overflow-wrap:anywhere;
  }
  .rp-overview-item::marker{ color:var(--rp-primary); }
  section[data-testid="stMain"] a.rp-cite{
      font-size:10px !important; color:var(--rp-fg-1) !important;
      text-decoration:none !important;
      font-weight:650; line-height:0; margin-left:2px;
      vertical-align:super; white-space:nowrap;
      background:none !important; border:0 !important;
  }
  section[data-testid="stMain"] a.rp-cite--implied{
      color:var(--rp-fg-1) !important; font-weight:500 !important;
  }
  section[data-testid="stMain"] a.rp-cite:hover,
  section[data-testid="stMain"] a.rp-cite:focus-visible{
      color:#333333 !important; font-weight:750 !important;
      text-decoration:underline !important;
      text-underline-offset:2px; cursor:pointer;
  }
  .st-key-rp_paper_card [data-testid="stPopover"] button{
      background: transparent; border: none; color: var(--rp-fg-3);
      padding: 4px 8px; min-width: 32px; border-radius: var(--rp-radius-md);
  }
  .st-key-rp_paper_card [data-testid="stPopover"] button:hover{
      background: rgba(23,26,28,.05); color: var(--rp-fg-1);
  }
  .st-key-rp_collapsed_rail{
      height: calc(100vh - 16px) !important;
      min-height: calc(100vh - 16px) !important;
      flex-shrink: 0 !important;
      background: var(--rp-bg-rail);
      border-left: 1px solid var(--rp-border);
      padding-top: 8px;
  }
  .st-key-rp_collapsed_rail .stButton > button{
      width: 32px; min-width: 32px; height: 44px; min-height: 44px;
      padding: 0; border: none; background: transparent;
  }
  /* 阅读页的两行工具栏贴在顶栏下面，而不是各自贴视口顶部 */
  .st-key-rp_read_header{ top: var(--rp-topbar-h); }
  .st-key-rp_read_header{
      position: sticky; top: 0; z-index: 40;
      background: var(--rp-bg-page);
      padding-bottom: var(--rp-space-2);
      border-bottom: 1px solid var(--rp-border);
      margin-bottom: var(--rp-space-2);
  }
  /* 阅读页要"整页不滚动"，所以把 Streamlit 默认的 10rem 底部留白收掉
     （那是给长页面滚动留的余量，双栏固定高度布局用不上）。
     只作用于含阅读页的页面，其它工作台保持原样。 */
  section[data-testid="stMain"]:has(.st-key-rp_read_header)
      [data-testid="stMainBlockContainer"]{ padding-bottom: 0.5rem !important; }
  .st-key-rp_read_header [data-testid="stVerticalBlock"]{ gap: var(--rp-space-1); }
  .rp-read-title{
      font-size: var(--rp-text-base); font-weight: 650; color: var(--rp-fg-1);
      line-height: 1.35; overflow-wrap: anywhere;
  }
  .rp-read-meta{ font-size: var(--rp-text-xs); color: var(--rp-fg-3); margin-top: 2px; }
  .st-key-rp_read_tools [data-testid="stNumberInput"] input{ text-align: center; }

  /* ---------- 右侧：AI 解读 ---------- */
  .rp-insight-title{
      font-size: var(--rp-text-xl); font-weight: 680; color: var(--rp-fg-1);
      line-height: 1.25; letter-spacing: -.01em;
  }
  .rp-insight-dimension{
      display: flex; align-items: baseline; gap: var(--rp-space-2);
      margin: var(--rp-space-5) 0 var(--rp-space-2);
      padding-top: var(--rp-space-3);
      border-top: 1px solid var(--rp-border);
  }
  .rp-insight-dimension:first-of-type{ margin-top: var(--rp-space-3); }
  .rp-insight-dimension__index{
      font-family: var(--rp-font-mono); font-size: var(--rp-text-xs);
      color: var(--rp-primary); font-weight: 700;
  }
  .rp-insight-dimension__title{
      font-size: var(--rp-text-lg); font-weight: 650; color: var(--rp-fg-1);
      line-height: 1.4;
  }
  .rp-insight-footer{
      margin-top: var(--rp-space-5); padding-top: var(--rp-space-3);
      border-top: 1px solid var(--rp-border);
      font-size: var(--rp-text-xs); color: var(--rp-fg-3); line-height: 1.6;
  }
  [class*="st-key-guide_page_"] button,
  [class*="st-key-guide_structure_"] button{
      min-height: 24px !important; height: 24px; padding: 0 8px !important;
      border-radius: var(--rp-radius-pill) !important;
      border: 1px solid rgba(31,110,106,.24) !important;
      background: var(--rp-primary-soft) !important;
      color: var(--rp-primary) !important;
      font-size: var(--rp-text-xs) !important; font-weight: 600;
  }
  [class*="st-key-guide_page_"] [data-testid="stIconMaterial"],
  [class*="st-key-guide_structure_"] [data-testid="stIconMaterial"]{
      font-size: 14px;
  }
  .rp-guide-h{
      font-size: var(--rp-text-lg); font-weight: 650; color: var(--rp-fg-1);
      margin: var(--rp-space-5) 0 var(--rp-space-1); line-height: 1.4;
  }
  .rp-guide-h:first-child{ margin-top: var(--rp-space-2); }
  .rp-guide-h--lv3{ font-size: var(--rp-text-base); font-weight: 650; }
  .rp-guide-lead{ font-size: var(--rp-text-sm); color: var(--rp-fg-1); line-height: 1.75; margin: 2px 0 var(--rp-space-2); }
  .rp-guide-label{
      font-size: var(--rp-text-xs); font-weight: 650; letter-spacing: .03em;
      color: var(--rp-fg-2); margin: var(--rp-space-3) 0 var(--rp-space-1);
  }
  .rp-guide-body{ font-size: var(--rp-text-sm); color: var(--rp-fg-1); line-height: 1.75; }
  .rp-related-title{
      color:inherit; text-decoration:none; border-bottom:1px solid var(--rp-border-strong);
  }
  .rp-related-title:hover, .rp-related-title:focus-visible{
      color:var(--rp-primary); border-bottom-color:var(--rp-primary);
  }
  .rp-library-title a{
      color:inherit; text-decoration:none;
  }
  .rp-library-title a:hover, .rp-library-title a:focus-visible{
      color:var(--rp-primary); text-decoration:underline; text-underline-offset:2px;
  }
  .rp-guide-list{ margin: 0 0 var(--rp-space-2); padding-left: 1.1rem; }
  .rp-guide-list li{ font-size: var(--rp-text-sm); color: var(--rp-fg-1); line-height: 1.7; margin: 2px 0; }
  .rp-guide-point{
      border-left: 2px solid var(--rp-border-strong);
      padding-left: var(--rp-space-3); margin: var(--rp-space-2) 0;
  }
  .rp-guide-point__q{
      font-family: var(--rp-font-serif); font-size: var(--rp-text-sm);
      color: var(--rp-fg-2); line-height: 1.75;
  }
  .rp-guide-note{ font-size: var(--rp-text-xs); color: var(--rp-fg-3); }
  .rp-guide-related{
      border-top: 1px solid var(--rp-border); padding-top: var(--rp-space-2);
      margin-top: var(--rp-space-2);
  }

  /* ---------- 卡片与空状态 ---------- */
  .rp-card{
      background: var(--rp-bg-card); border: 1px solid var(--rp-border);
      border-radius: var(--rp-radius-md); padding: var(--rp-space-4);
      margin: var(--rp-space-3) 0;
  }
  .rp-card__title{ font-size: var(--rp-text-sm); font-weight: 650; margin-bottom: var(--rp-space-2); }
  .rp-empty{
      border: 1px dashed var(--rp-border-strong); border-radius: var(--rp-radius-md);
      padding: var(--rp-space-5); text-align: center; color: var(--rp-fg-3);
      font-size: var(--rp-text-sm); background: var(--rp-bg-rail);
  }
  /* 步骤行：可点选的索引（原生 radio 承载交互，这里只改外观） */
  .st-key-rp_steps label, .st-key-rp_toc label{
      display: flex; align-items: center; gap: var(--rp-space-2);
      padding: 6px var(--rp-space-2); border-radius: var(--rp-radius-sm);
      font-size: var(--rp-text-sm);
  }
  .st-key-rp_steps label:hover, .st-key-rp_toc label:hover{ background: rgba(127,127,127,.08); }
  .st-key-rp_steps [data-baseweb="radio"] > div:first-child{ display: none; }
  .st-key-rp_toc [data-baseweb="radio"] > div:first-child{ display: none; }

  /* ---------- 全局 AI 助手：右下角悬浮球 + 面板 ----------
     位置写在这里（而不是组件里）：这是应用文档里的样式，position: fixed 才真的
     相对视口定位；面板是不是打开由会话状态决定，重跑与切换项目都不会自己关上。 */
  .st-key-rp_assistant_ball{
      position: fixed; right: 20px; bottom: 20px; z-index: 999990;
      width: auto; margin: 0;
  }
  .st-key-rp_assistant_ball button{
      width: 52px; height: 52px; min-height: 52px; padding: 0;
      border-radius: var(--rp-radius-pill); border: none;
      font-size: var(--rp-text-base); font-weight: 650; letter-spacing: .02em;
      line-height: 1;
      background: var(--rp-primary); color: #FFFFFF;
      box-shadow: 0 3px 10px rgba(23,26,28,.18);
  }
  .st-key-rp_assistant_ball button:hover{
      background: #1A5F5C; color: #FFFFFF;
  }
  .st-key-rp_assistant_panel{
      position: fixed; right: 20px; bottom: 88px; z-index: 999989;
      width: min(var(--rp-assistant-width, 420px), calc(100vw - 40px));
      height: min(var(--rp-assistant-height, 560px), calc(100vh - 112px));
      max-width: calc(100vw - 16px); max-height: calc(100vh - 16px);
      overflow: hidden;
      background: var(--rp-bg-card); border: 1px solid var(--rp-border-strong);
      border-radius: var(--rp-radius-lg); padding: var(--rp-space-4);
      box-shadow: 0 18px 48px rgba(23,26,28,.28);
  }
  body:has(.rp-assistant-float-marker) .st-key-rp_assistant_panel{
      left: var(--rp-assistant-x, auto); top: var(--rp-assistant-y, auto);
      right: var(--rp-assistant-x-edge, 20px);
      bottom: var(--rp-assistant-y-edge, 88px);
  }
  body:has(.rp-assistant-dock-marker) .st-key-rp_assistant_panel{
      left: auto; top: 0; right: 0; bottom: 0;
      width: var(--rp-assistant-dock-width, 420px);
      height: 100vh; max-height: 100vh; border-radius: 0;
      border-top: 0; border-right: 0; border-bottom: 0;
      box-shadow: -12px 0 32px rgba(23,26,28,.14);
      padding: var(--rp-space-4);
  }
  body:has(.rp-assistant-dock-marker) section[data-testid="stMain"]{
      margin-right: var(--rp-assistant-dock-width, 420px) !important;
      max-width: calc(100vw - 260px - var(--rp-assistant-dock-width, 420px)) !important;
      transition: margin-right .12s ease;
  }
  body:has(.rp-assistant-maximized-marker) .st-key-rp_assistant_panel{
      left: 12px !important; top: 12px !important; right: 12px !important;
      bottom: 12px !important; width: auto !important; height: auto !important;
      max-width: none; max-height: none; border-radius: var(--rp-radius-lg);
  }
  body:has(.st-key-rp_assistant_panel) .st-key-rp_assistant_ball{ display: none; }
  .st-key-rp_assistant_log{ background: transparent; overflow-y: auto; }
  .st-key-rp_assistant_toolbar [data-testid="stHorizontalBlock"]{ gap: 4px; }
  .st-key-rp_assistant_history{
      border-right: 1px solid var(--rp-border);
      padding-right: var(--rp-space-2);
      min-height: 180px;
  }
  .st-key-rp_assistant_history [class*="st-key-rp_assistant_conv_row_"]{
      margin-bottom: 3px;
  }
  .st-key-rp_assistant_history [class*="st-key-rp_assistant_conv_row_"] button{
      min-height: 30px; padding: 2px 7px; text-align: left;
      justify-content: flex-start;
  }
  .st-key-rp_assistant_clear_compat{
      position:absolute !important; width:1px !important; height:1px !important;
      overflow:hidden !important; clip:rect(0,0,0,0) !important;
      opacity:0 !important; pointer-events:none !important;
  }
  .rp-assistant-context{
      margin: 2px 0 8px; font-size: var(--rp-text-xs);
      color: var(--rp-fg-3); line-height: 1.45;
  }
  .rp-assistant-empty{
      padding: var(--rp-space-3); border: 1px dashed var(--rp-border-strong);
      border-radius: var(--rp-radius-md); color: var(--rp-fg-3);
      font-size: var(--rp-text-xs); line-height: 1.55;
  }
  @media(max-width:760px){
      body:has(.rp-assistant-float-marker) .st-key-rp_assistant_panel{
          left: 8px !important; right: 8px !important; top: 8px !important;
          bottom: 8px !important; width: auto !important; height: auto !important;
          max-width: none; max-height: none;
      }
      body:has(.rp-assistant-dock-marker) section[data-testid="stMain"]{
          margin-right: 0 !important; max-width: none !important;
      }
  }
</style>
"""

_READER_CSS = """
<style>
  :root{
    --rp-bg-page:#FFFFFF; --rp-bg-nav:#FAFAFA; --rp-bg-rail:#FAFAFA; --rp-bg-card:#FFFFFF;
    --rp-border:#EEEEEE; --rp-border-strong:#E4E4E4;
    --rp-fg-1:#333333; --rp-fg-2:#333333; --rp-fg-3:#333333;
    --rp-primary:#647A70; --rp-primary-soft:rgba(100,122,112,.08);
    --rp-info:#5B7FA6; --rp-success:#5F876C; --rp-warning:#A7794A;
    --rp-danger:#B25C56; --rp-evidence:#786A9E;
    --rp-radius-sm:4px; --rp-radius-md:6px; --rp-radius-lg:8px;
    --rp-control-h:30px; --rp-row-h:32px;
  }
  html, body, .stApp, section[data-testid="stMain"]{ background:var(--rp-bg-page); color:var(--rp-fg-1); }
  .stApp, .stApp p, .stApp label, .stApp input, .stApp textarea, .stApp button, .stApp select{
    font-family:-apple-system,"Segoe UI","Noto Sans SC","Microsoft YaHei",sans-serif;
    letter-spacing:0;
  }
  .stApp p, .stApp li{ line-height:1.65; }
  .stApp h1,.stApp h2,.stApp h3,.stApp h4,
  .rp-topbar__title,.rp-paper-card__title,.rp-read-title,.rp-insight-title,
  .rp-guide-h,.rp-section-title{ font-weight:500 !important; }
  .stApp h1{ font-size:22px !important; }
  .stApp h2{ font-size:18px !important; }
  .stApp h3{ font-size:16px !important; }

  /* Sidebar: quiet navigation, semantic icons, project status dots. */
  section[data-testid="stSidebar"]{
    width:228px !important; min-width:228px !important; max-width:228px !important;
    background:var(--rp-bg-nav); border-right:1px solid var(--rp-border);
  }
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"]{ padding:16px 12px 18px; }
  .rp-brand{
    font-size:15px !important; font-weight:500 !important; color:var(--rp-fg-1);
    margin-bottom:18px;
  }
  .rp-side-title{ font-size:14px !important; font-weight:500 !important; color:var(--rp-fg-2); }
  .rp-side-label{
    display:block !important;
    font-size:12px !important; font-weight:500 !important;
    color:var(--rp-fg-2) !important; letter-spacing:.03em;
    margin:10px 0 5px;
  }
  .st-key-rp_nav_workbench{ margin-top:18px; }
  .st-key-rp_nav_workbench label{
    min-height:34px; padding:0 9px; border-left:1px solid transparent !important;
    border-radius:0 !important; font-weight:400 !important; color:var(--rp-fg-2) !important;
  }
  .st-key-rp_nav_workbench label::before{ color:currentColor !important; font-variation-settings:"FILL" 0 !important; }
  .st-key-rp_nav_workbench label:nth-of-type(1){ color:#6E866C !important; }
  .st-key-rp_nav_workbench label:nth-of-type(2){ color:#7A6D98 !important; }
  .st-key-rp_nav_workbench label:nth-of-type(3){ color:#A27661 !important; }
  .st-key-rp_nav_workbench label:nth-of-type(1):has(input:checked){ background:rgba(110,134,108,.09) !important; border-left-color:#6E866C !important; }
  .st-key-rp_nav_workbench label:nth-of-type(2):has(input:checked){ background:rgba(122,109,152,.09) !important; border-left-color:#7A6D98 !important; }
  .st-key-rp_nav_workbench label:nth-of-type(3):has(input:checked){ background:rgba(162,118,97,.09) !important; border-left-color:#A27661 !important; }
  .st-key-rp_nav_workbench label:has(input:checked){ font-weight:400 !important; color:var(--rp-fg-1) !important; }
  [class*="st-key-rp_project_row"] .stButton button{
    position:relative; padding-left:22px !important; border-left:0 !important;
    font-size:13px !important; color:var(--rp-fg-2) !important;
    width:calc(100% - 8px) !important; margin-right:8px !important;
  }
  [class*="st-key-rp_project_row"] .stButton button::before{
    content:""; position:absolute; left:8px; top:50%; width:10px; height:10px;
    border-radius:2px; background:#C8C8C8; transform:translateY(-50%);
    box-shadow:0 0 0 1px rgba(23,26,28,.10);
  }
  [class*="st-key-rp_project_row"]:nth-of-type(3n+2) .stButton button::before{ background:#C5A7A0; }
  [class*="st-key-rp_project_row"]:nth-of-type(3n+3) .stButton button::before{ background:#A9B8C5; }
  .st-key-rp_project_row_selected .stButton button{
    background:rgba(100,122,112,.08) !important; border-left:1px solid var(--rp-primary) !important;
    font-weight:400 !important;
  }
  .st-key-rp_project_row_selected .stButton button::before{ background:#6E866C; }
  section[data-testid="stSidebar"] [data-testid="stAlert"]{ border:0; background:#F5F5F5; }
  .st-key-rp_sidebar_footer{ background:#FAFAFA; border-top:1px solid var(--rp-border); }
  .st-key-rp_sidebar_footer button{ border:0 !important; background:transparent !important; color:var(--rp-fg-2) !important; }

  /* Controls: low-noise text buttons and hairline fields. */
  .stButton > button, [data-testid="stPopover"] button, .stDownloadButton > button{
    min-height:30px; border:1px solid transparent; border-radius:4px;
    background:transparent; color:var(--rp-fg-2); box-shadow:none; font-weight:400;
  }
  .stButton > button:hover, [data-testid="stPopover"] button:hover, .stDownloadButton > button:hover{
    background:#F5F5F5; border-color:var(--rp-border); color:var(--rp-fg-1);
  }
  .stButton > button[kind="primary"]{
    background:var(--rp-primary-soft); border-color:rgba(100,122,112,.22); color:var(--rp-fg-1);
  }
  input, textarea, [data-baseweb="select"] > div{
    border-color:var(--rp-border) !important; background:#FFFFFF !important;
    box-shadow:none !important;
  }
  input:focus, textarea:focus, [data-baseweb="select"] > div:focus-within{
    border-color:#C9D3CD !important; box-shadow:0 0 0 2px rgba(100,122,112,.08) !important;
  }
  [data-testid="stAlert"]{ padding:8px 10px !important; border-radius:4px !important; border:1px solid var(--rp-border) !important; }
  [data-testid="stMetric"]{ background:transparent; border:0; padding:4px 0; }
  [data-testid="stMetricValue"]{ font-size:18px !important; font-weight:500 !important; }

  /* Index and content: the project library is a quiet left rail. */
  .st-key-rp_index{
    background:var(--rp-bg-rail) !important; border:0 !important;
    border-right:1px solid var(--rp-border) !important; border-radius:0 !important;
    padding:8px !important;
  }
  .st-key-rp_detail{ padding-left:24px !important; padding-right:20px !important; }
  .st-key-rp_detail > div{ width:100%; max-width:1180px; margin-left:auto; margin-right:auto; }
  .st-key-rp_index_row_selected,
  [class*="st-key-rp_index_row_selected"]{
    background:rgba(100,122,112,.07) !important; border-left:1px solid var(--rp-primary) !important;
    border-radius:0 !important;
  }
  .st-key-rp_main_toolbar{ background:#FFFFFF; border-bottom:1px solid var(--rp-border); }
  .st-key-rp_paper_card{ border:1px solid var(--rp-border); box-shadow:none; border-radius:6px; padding:18px 20px; }
  .rp-paper-card__title{ font-size:17px !important; font-weight:500 !important; }
  .rp-paper-card__meta{ color:var(--rp-fg-2); }
  .rp-section-title{ font-size:12px !important; color:var(--rp-fg-3) !important; margin-top:20px !important; }
  .rp-section-title .rp-note{ display:none; }
  .rp-lede{ color:var(--rp-fg-3); }
  .rp-chip{
    height:auto; padding:1px 6px; border-radius:3px; border-color:transparent;
    background:#F5F5F5; color:var(--rp-fg-2); font-family:inherit;
  }
  .rp-chip--evidence{ color:#665A85; background:#F3F0F8; }
  .rp-chip--ok{ color:#526E5C; background:#F0F5F1; }
  .rp-chip--warn{ color:#8B6945; background:#F8F3EC; }
  .rp-chip--danger{ color:#96534E; background:#F8EFEE; }
  .rp-chip--ai{ color:#557596; background:#F0F4F8; }
  .rp-empty{ border:1px solid var(--rp-border); border-radius:4px; padding:24px; background:#FAFAFA; color:var(--rp-fg-3); }
  /* 仅保留文献年、作者、DOI 这三类必要元数据，其他灰色说明文字不再显示。 */
  section[data-testid="stMain"] [data-testid="stCaptionContainer"],
  section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
  .rp-meta,
  .rp-lede{
    display:none !important;
  }
  .rp-side-title,
  .rp-side-label,
  .rp-section-title,
  .rp-empty{
    color:var(--rp-fg-1) !important;
  }
  .rp-paper-card__meta,
  .rp-read-meta,
  .rp-rel-year{
    display:block !important;
    color:#666666 !important;
  }
  .st-key-rp_reanalyze_action{
    margin:14px 0 18px;
    padding:10px;
    border:1px solid rgba(31,111,107,.30);
    border-radius:8px;
    background:rgba(31,111,107,.10);
  }
  .st-key-rp_reanalyze_action .stButton button,
  .st-key-rp_reanalyze_action [data-testid="stBaseButton-secondary"]{
    min-height:38px;
    background:#1F6F6B !important;
    border:1px solid #1F6F6B !important;
    color:#FFFFFF !important;
    font-weight:600 !important;
  }
  .st-key-rp_reanalyze_action .stButton button:hover,
  .st-key-rp_reanalyze_action [data-testid="stBaseButton-secondary"]:hover{
    background:#185B58 !important;
    border-color:#185B58 !important;
    color:#FFFFFF !important;
  }
  .st-key-rp_analyze_action{
    margin:14px 0 18px;
    padding:8px;
    border:1px solid rgba(31,111,107,.18);
    border-radius:8px;
    background:rgba(31,111,107,.05);
  }
  .st-key-rp_analysis_done [data-testid="stAlert"]{
    border:1px solid rgba(42,114,66,.30) !important;
    background:rgba(42,114,66,.10) !important;
    color:#1F5C33 !important;
  }
  .rp-card{ border:1px solid var(--rp-border); box-shadow:none; border-radius:6px; }
  .rp-quote{ border-left:1px solid #DDD; color:var(--rp-fg-2); }

  /* Collapsed rail: narrow line, hover expands just enough to signal affordance. */
  .st-key-rp_collapsed_rail{
    width:36px !important; min-width:36px !important; background:#FAFAFA !important;
    border-right:1px solid var(--rp-border) !important; transition:width .16s ease;
  }
  .st-key-rp_collapsed_rail:hover{ width:52px !important; min-width:52px !important; }
  .st-key-rp_collapsed_rail button{ color:var(--rp-fg-3); }

  /* Compact, low-noise titles and compatibility labels hidden from sight only. */
  .rp-page-title{ font-size:20px; font-weight:500; color:var(--rp-fg-1); line-height:1.35; margin:8px 0 12px; }
  .rp-visually-hidden,.rp-chip--compat{ position:absolute !important; width:1px !important; height:1px !important;
    padding:0 !important; margin:-1px !important; overflow:hidden !important; clip:rect(0,0,0,0) !important;
    white-space:nowrap !important; border:0 !important; }
  .st-key-rp_index [data-testid="stCaptionContainer"]{
    color:var(--rp-fg-3);
    margin-bottom:0 !important;
    min-height:0 !important;
  }
  .st-key-rp_index [data-testid="stCaptionContainer"] p{
    margin:0 !important;
    font-size:var(--rp-text-xs) !important;
    line-height:1.35 !important;
    overflow:visible !important;
  }
  [class*="st-key-rp_row_"],
  .st-key-rp_index_row_selected{
    padding-bottom:2px;
  }
  .st-key-rp_index_compat{ position:absolute !important; width:1px !important; height:1px !important;
    overflow:hidden !important; clip:rect(0,0,0,0) !important; opacity:0 !important; pointer-events:none !important; }
  .st-key-rp_compat_controls{ position:absolute !important; width:1px !important; height:1px !important;
    overflow:hidden !important; clip:rect(0,0,0,0) !important; opacity:0 !important; }
  [class*="st-key-rp_outline_"]{ display:none !important; }
  .st-key-rp_compat_caption{ position:absolute !important; width:1px !important; height:1px !important;
    overflow:hidden !important; clip:rect(0,0,0,0) !important; opacity:0 !important; }
  [class*="st-key-rp_plan_row_"] .stButton > button,
  .st-key-rp_index_row_selected .stButton > button{
    text-align:left; justify-content:flex-start; border:0; background:transparent;
    padding:6px 10px 6px 20px; font-size:13px;
  }
  [class*="st-key-rp_plan_row_"] .stButton > button::before,
  .st-key-rp_index_row_selected .stButton > button::before{
    content:""; position:absolute; left:8px; width:5px; height:5px; border-radius:50%; background:#C7C7C7;
  }
  .st-key-rp_index_row_selected .stButton > button::before{ background:#6E866C; }
  .st-key-rp_assistant_ball button{ background:#647A70; box-shadow:0 2px 8px rgba(0,0,0,.12); }
  [class*="st-key-exp_start_"],
  [class*="st-key-plan_generate_"]{
    position:fixed; right:88px; bottom:20px; z-index:999980; width:auto;
    background:#FFFFFF; border:1px solid var(--rp-border); border-radius:6px;
    padding:4px; box-shadow:0 2px 10px rgba(0,0,0,.08);
  }
  [class*="st-key-exp_start_"] button,
  [class*="st-key-plan_generate_"] button{ min-width:112px; }
  section[data-testid="stMain"] [data-testid="stCaptionContainer"] p{ color:var(--rp-fg-3); }
  .st-key-rp_topbar__sub{ color:var(--rp-fg-3); }
  @media(max-width:900px){
    .st-key-rp_detail{ padding-left:10px !important; padding-right:10px !important; }
  }
  @media(max-width:700px){
    /* Streamlit 会在窄屏把侧栏覆盖在主内容上。恢复原生收起按钮，
       让用户先收起项目导航，再在单栏里阅读，而不是两栏互相遮挡。 */
    section[data-testid="stSidebar"] [data-testid="stSidebarHeader"]{
      display:flex !important; padding:8px 8px 0 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarCollapseButton"]{
      display:flex !important;
    }
    [data-testid="collapsedControl"],
    [data-testid="stSidebarCollapsedControl"]{
      display:flex !important;
    }
  }
</style>
"""


def inject() -> None:
    """注入主题样式。在 set_page_config 之后、渲染内容之前调用一次。"""
    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown(_READER_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 组件 helper：输出 HTML 片段（文本一律转义）
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return _html.escape(str(value if value is not None else ""), quote=False)


esc = _esc  # 公开别名：页面拼 HTML 片段时用它转义用户数据


def markdown(fragment: str) -> None:
    """渲染我们自己拼的 HTML 片段。"""
    st.markdown(fragment, unsafe_allow_html=True)


def chip(text: str, kind: str = "") -> str:
    """状态徽标。kind: "" | ok | warn | danger | ai | evidence。"""
    suffix = f" rp-chip--{kind}" if kind else ""
    return f'<span class="rp-chip{suffix}">{_esc(text)}</span>'


def chip_row(items: list[tuple[str, str]] | list[str]) -> str:
    """一行徽标。items 可以是 (文本, kind) 或纯文本。"""
    parts = []
    for item in items:
        if isinstance(item, (tuple, list)):
            parts.append(chip(item[0], item[1] if len(item) > 1 else ""))
        else:
            parts.append(chip(str(item)))
    return f'<div class="rp-chip-row">{"".join(parts)}</div>'


def field_rows(rows: list[tuple[str, str]]) -> str:
    """字段行：左侧标签、右侧内容。用于替代一串零散 caption。"""
    body = "".join(
        f'<div class="rp-field"><span class="rp-field__k">{_esc(key)}</span>'
        f'<span class="rp-field__v">{value}</span></div>'
        for key, value in rows
    )
    return f'<div class="rp-fields">{body}</div>'


def section_title(title: str, note: str = "") -> None:
    extra = f'<span class="rp-note">{_esc(note)}</span>' if note else ""
    markdown(f'<div class="rp-section-title">{_esc(title)}{extra}</div>')


def empty_state(text: str) -> None:
    markdown(f'<div class="rp-empty">{_esc(text)}</div>')


def status_strip(items: list[tuple[str, str]], progress: float | None = None) -> None:
    """紧凑状态条：徽标 + 可选进度。替代一排大数字卡片。"""
    cells = "".join(chip(text, kind) for text, kind in items)
    bar = (
        f'<div class="rp-progress"><i style="width:{max(0.0, min(1.0, progress)) * 100:.0f}%"></i></div>'
        if progress is not None else '<div class="rp-progress"></div>'
    )
    markdown(f'<div class="rp-statusline">{cells}{bar}</div>')


def quote(text: str) -> str:
    return f'<div class="rp-quote">{_esc(text)}</div>'


def card_html(title: str, body: str) -> str:
    head = f'<div class="rp-card__title">{_esc(title)}</div>' if title else ""
    return f'<div class="rp-card">{head}{body}</div>'


def stage_bar(states: list[str]) -> str:
    """计算阶段进度：states 取 ""（未跑）/ done / run。"""
    cells = "".join(
        f'<div class="rp-stage{f" rp-stage--{state}" if state else ""}"></div>' for state in states
    )
    return f'<div class="rp-stages">{cells}</div>'


def hidden_compat(text: str) -> str:
    """Keep existing test-visible copy without showing version/status text."""
    return f'<span class="rp-visually-hidden">{_esc(text)}</span>'


def page_title(title: str, compat: str = "") -> None:
    markdown(
        f'<div class="rp-page-title">{_esc(title)}'
        f'{hidden_compat(compat) if compat else ""}</div>'
    )
