"""Interactive citation graph workspace built with Streamlit v2 components."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from ..literature import (
    citations as cite_mod,
    graph_exports,
    graph_preferences,
    graph_recommendations,
    graph_versions,
    library,
)
from ..storage import repository as repo
from ..storage.models import (
    Certainty,
    Paper,
    PaperRelation,
    Project,
    RelationSource,
    RelationType,
    ReviewStatus,
)
from . import browser_prefs


_HTML = """
<div class="cg" aria-label="引用关系图谱工作区">
  <div class="cg__tools">
    <label class="cg__search">
      <span>搜索标题或作者</span>
      <input type="search" placeholder="输入关键词，回车切换结果" autocomplete="off">
    </label>
    <span class="cg__search-count" aria-live="polite">0 / 0</span>
  </div>
  <div class="cg__legend">
    <span><i class="dot official"></i>官方引用</span>
    <span><i class="dot manual"></i>人工增补</span>
    <span><i class="dot candidate"></i>候选</span>
    <span><i class="dot pending"></i>待判断</span>
    <span><i class="dot rejected"></i>已驳回</span>
    <span><i class="dot recommended"></i>算法推荐 · 待核验</span>
  </div>
  <div class="cg__viewport">
    <div class="cg__canvas" tabindex="0" aria-label="图谱空白区，右键可添加项目内文献">
      <section class="cg__group" data-group="method_source">
        <h4>方法来源</h4><div class="cg__nodes"></div>
      </section>
      <div class="cg__center"><div class="cg__current"></div></div>
      <section class="cg__group" data-group="later_extension">
        <h4>后续延伸</h4><div class="cg__nodes"></div>
      </section>
      <section class="cg__related" data-group="related">
        <h4>相关研究</h4><div class="cg__nodes"></div>
      </section>
    </div>
    <div class="cg__timeline" hidden>
      <svg class="cg__edges" aria-hidden="true">
        <defs>
          <marker id="cg-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 z"></path>
          </marker>
        </defs>
      </svg>
      <div class="cg__timeline-nodes"></div>
    </div>
    <div class="cg__zoom" aria-label="画布缩放控制">
      <button type="button" data-zoom="out" title="缩小">−</button>
      <button type="button" data-zoom="reset" title="复位">复位</button>
      <button type="button" data-zoom="in" title="放大">+</button>
    </div>
  </div>
  <details class="cg__recommendations" hidden>
    <summary>拓展推荐 · 算法推荐待核验</summary>
    <div class="cg__recommendation-list"></div>
  </details>
  <div class="cg__menu" role="menu" hidden></div>
  <div class="cg__toast" role="status" aria-live="polite" hidden></div>
  <div class="cg__notice" role="status" aria-live="polite" hidden></div>
</div>
"""


_CSS = """
:host{font-family:-apple-system,"Segoe UI","Noto Sans SC","Microsoft YaHei",sans-serif;color:#26302C}
.cg{position:relative;width:100%}
.cg__tools{display:flex;align-items:center;gap:8px;margin:0 0 8px}
.cg__search{display:flex;align-items:center;gap:8px;min-width:0;flex:1 1 320px}
.cg__search span{font-size:11px;color:#66736D;white-space:nowrap}
.cg__search input{
  width:min(360px,100%);min-height:32px;padding:5px 9px;border:1px solid #D8E0DC;
  border-radius:6px;background:#fff;color:#26302C;font-size:12px;outline:none;
}
.cg__search input:focus{border-color:#2A746D;box-shadow:0 0 0 2px rgba(42,116,109,.12)}
.cg__search-count{font-size:11px;color:#66736D;white-space:nowrap}
.cg__legend{display:flex;flex-wrap:wrap;gap:10px 16px;margin:2px 0 10px;font-size:11px;color:#66736D}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.dot.official{background:#1F6E6A}.dot.manual{background:#fff;border:2px solid #1F6E6A}
.dot.candidate{background:#E8ECEA;border:1px solid #B8C1BC}.dot.pending{background:#D5A75C}
.dot.rejected{background:#D9DEDB;border:1px dashed #89948E}
.dot.recommended{background:#F1F3F2;border:1px dashed #89948E}
.cg__viewport{
  position:relative;min-height:420px;overflow:hidden;border:1px solid #E3E8E5;
  border-radius:9px;background:#FCFDFC;
}
.cg__canvas{
  position:relative;display:grid;grid-template-columns:minmax(180px,1fr) 190px minmax(180px,1fr);
  grid-template-rows:auto auto;gap:14px 20px;min-height:390px;padding:12px;
  transform-origin:0 0;background:#FCFDFC;
}
.cg__group,.cg__related{min-width:0}
.cg__group{min-height:86px}
.cg__group h4,.cg__related h4{margin:0 0 8px;font-size:11px;font-weight:600;color:#67736D;letter-spacing:.04em}
.cg__related{grid-column:1 / -1;border-top:1px dashed #E1E6E3;padding-top:10px}
.cg__related .cg__nodes{display:flex;flex-wrap:wrap;gap:8px}
.cg__related .node{flex:1 1 210px}
.cg__nodes{display:flex;flex-direction:column;gap:8px}
.cg__group[data-group="method_source"]{grid-column:1}.cg__center{grid-column:2;grid-row:1 / span 2;display:flex;align-items:center;justify-content:center}
.cg__group[data-group="later_extension"]{grid-column:3;grid-row:1}
.cg__related{grid-row:2}
.node{
  position:relative;padding:9px 10px;border:1px solid #DDE4E0;border-radius:7px;
  background:#fff;cursor:pointer;transition:transform .12s ease,box-shadow .12s ease;
  user-select:none;min-width:0;
}
.node:hover,.node:focus-visible{transform:scale(1.02);box-shadow:0 4px 14px rgba(29,50,40,.12);outline:none;z-index:3}
.node.official{background:#1F5F5B;border-color:#1F5F5B;color:#fff}
.node.manual{border:2px solid #315F56;background:#fff;color:#26302C}
.node.candidate{background:#F3F5F4;border-color:#C7CFCA;color:#4E5B55}
.node.pending{background:#FFF9ED;border-color:#DAB77C;color:#5A4730}
.node.rejected{background:#F7F8F8;border:1px dashed #9AA59F;opacity:.55;color:#68736E}
.node.recommended{background:#F3F4F3;border:1px dashed #919B96;color:#4D5853}
.node.is-focus{box-shadow:0 0 0 3px rgba(42,116,109,.20);border-color:#2A746D}
.node.is-selected{outline:3px solid #285FCA;outline-offset:2px;z-index:8}
.node.is-search-match{transform:scale(1.045);box-shadow:0 0 0 2px #8A5200;z-index:7}
.node.is-search-current{outline:3px solid #8A5200;outline-offset:3px}
.node.is-search-muted{opacity:.24}
.node__top{display:flex;align-items:flex-start;gap:6px}
.node__title{font-size:12px;font-weight:600;line-height:1.35;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.node__year{font-size:10px;opacity:.75;white-space:nowrap;margin-left:auto}
.node__note{margin-top:4px;font-size:10px;line-height:1.3;color:#7A857F;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node.official .node__note{color:#D7E8E5}
.node__badge{display:inline-block;margin-top:5px;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,.16);font-size:9px}
.node.manual .node__badge,.node.candidate .node__badge,.node.pending .node__badge,.node.rejected .node__badge,.node.recommended .node__badge{background:rgba(31,95,91,.08)}
.tool{position:absolute;left:8px;top:calc(100% + 7px);z-index:20;display:none;width:min(310px,80vw);padding:9px 10px;border:1px solid #DCE3DF;border-radius:6px;background:#fff;color:#26302C;box-shadow:0 10px 26px rgba(20,35,28,.18);font-size:11px;line-height:1.55;text-align:left}
.node:hover .tool,.node:focus-visible .tool{display:block}
.tool b{display:block;margin-bottom:3px}
.cg__current{width:100%;padding:20px 14px;border-radius:11px;background:#163F3C;color:#fff;text-align:center;box-shadow:0 8px 24px rgba(22,63,60,.18)}
.cg__current small{display:block;opacity:.7;margin-bottom:6px}.cg__current b{font-size:13px;line-height:1.5}
.cg__group.is-over,.cg__related.is-over{outline:2px solid #2A746D;outline-offset:4px;border-radius:7px}
.cg__timeline{
  position:relative;width:1100px;height:600px;transform-origin:0 0;
  background:linear-gradient(to bottom,#FCFDFC 0,#FCFDFC 100%);
}
.cg__edges{position:absolute;inset:0;width:1100px;height:600px;overflow:visible;pointer-events:none}
.cg__edges line{stroke:#AEBAB4;stroke-width:1.3}
.cg__edges line.recommended{stroke:#AEBAB4;stroke-dasharray:5 4}
.cg__edges marker path{fill:#8E9A94}
.cg__timeline-nodes{position:absolute;inset:0}
.cg__timeline-nodes .node{position:absolute;width:210px;min-height:58px}
.cg__timeline-nodes .node.current{background:#163F3C;border-color:#163F3C;color:#fff}
.cg__zoom{
  position:absolute;right:10px;bottom:10px;display:flex;gap:3px;padding:3px;
  border:1px solid #DDE3DF;border-radius:6px;background:rgba(255,255,255,.94);
  box-shadow:0 4px 14px rgba(20,35,28,.10);z-index:30;
}
.cg__zoom button{
  min-width:32px;height:28px;padding:0 7px;border:0;border-radius:4px;
  background:transparent;color:#37423D;font-size:12px;cursor:pointer;
}
.cg__zoom button:hover,.cg__zoom button:focus-visible{background:#EFF4F1;outline:none}
.cg__recommendations{margin-top:8px;border-top:1px dashed #DDE3DF;padding-top:8px;font-size:11px}
.cg__recommendations summary{cursor:pointer;color:#53645C;font-weight:600}
.cg__recommendation-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:6px;margin-top:8px}
.cg__recommendation{display:flex;align-items:center;gap:6px;padding:7px 8px;border:1px dashed #B7C0BB;border-radius:6px;background:#F5F6F5}
.cg__recommendation span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cg__recommendation button{border:1px solid #C9D1CD;border-radius:4px;background:#fff;font-size:10px;padding:3px 6px;cursor:pointer}
.cg__menu{position:absolute;z-index:50;min-width:184px;padding:5px;border:1px solid #DDE3DF;border-radius:6px;background:#fff;box-shadow:0 12px 28px rgba(20,35,28,.2)}
.cg__menu button{display:block;width:100%;padding:7px 9px;border:0;border-radius:4px;background:transparent;color:#37423D;text-align:left;font-size:12px;cursor:pointer}
.cg__menu button:hover,.cg__menu button:focus-visible{background:#F1F5F3;outline:none}
.cg__menu button.danger{color:#A04F49}.cg__menu button.confirm{background:#F7ECE9;color:#8E3F39;font-weight:600}
.cg__toast{position:absolute;left:50%;bottom:8px;transform:translateX(-50%);padding:6px 10px;border-radius:5px;background:#26302C;color:#fff;font-size:11px;z-index:60}
.cg__toast[hidden]{display:none}
.cg__notice{
  position:absolute;right:12px;top:48px;z-index:70;display:flex;align-items:center;gap:10px;
  max-width:min(440px,calc(100% - 24px));padding:10px 12px;border:1px solid #B9D0C1;
  border-radius:7px;background:#F5FAF6;color:#274337;box-shadow:0 10px 28px rgba(20,35,28,.18);
  font-size:12px;
}
.cg__notice[hidden]{display:none}
.cg__notice button{border:0;border-radius:4px;background:#2A746D;color:#fff;padding:6px 9px;font-size:11px;cursor:pointer;white-space:nowrap}
.cg.is-timeline .cg__canvas{display:none}.cg.is-timeline .cg__timeline{display:block}
@media(max-width:760px){
  .cg__tools{align-items:flex-start;flex-direction:column;gap:4px}
  .cg__search{width:100%;align-items:flex-start;flex-direction:column;gap:3px}
  .cg__canvas{grid-template-columns:1fr}.cg__center{grid-column:1;grid-row:1;order:-1}
  .cg__group[data-group="method_source"],.cg__group[data-group="later_extension"],.cg__related{grid-column:1;grid-row:auto}
}
"""


_JS = """
export default function(component) {
  const {data, setTriggerValue, parentElement} = component;
  const root = parentElement.querySelector(".cg");
  if (!root) return;
  const config = data && typeof data === "object" ? data : {};
  const current = config.current || {};
  const nodes = Array.isArray(config.nodes) ? config.nodes : [];
  const recommendations = Array.isArray(config.recommendations) ? config.recommendations : [];
  const recommendationNodes = recommendations.map((item) => ({
    ...item,
    relation_id: `rec:${item.openalex_id}`,
    status: "recommended",
    status_label: `推荐 · ${(item.sources || []).join(" + ") || "待核验"}`,
    relation_label: "算法推荐 · 待核验",
    graph_group: "related",
    side: "right",
    paper_in_library: false,
    recommended: true,
    version_editable: false,
    allowed_groups: [],
  }));
  const allItems = [...nodes, ...recommendationNodes];
  const canvas = root.querySelector(".cg__canvas");
  const viewport = root.querySelector(".cg__viewport");
  const timeline = root.querySelector(".cg__timeline");
  const timelineSvg = root.querySelector(".cg__edges");
  const timelineNodes = root.querySelector(".cg__timeline-nodes");
  const menu = root.querySelector(".cg__menu");
  const toast = root.querySelector(".cg__toast");
  const notice = root.querySelector(".cg__notice");
  const currentBox = root.querySelector(".cg__current");
  const groups = [...canvas.querySelectorAll("[data-group]")];
  const search = root.querySelector(".cg__search input");
  const searchCount = root.querySelector(".cg__search-count");
  const recommendationsBox = root.querySelector(".cg__recommendations");
  const recommendationList = root.querySelector(".cg__recommendation-list");
  const layout = config.layout === "timeline" ? "timeline" : "radial";
  const activeStage = layout === "timeline" ? timeline : canvas;
  let toastTimer = null;
  let noticeTimer = null;
  let dragItem = null;
  let panning = false;
  let panStart = null;
  let scale = 1;
  let tx = 0;
  let ty = 0;
  let searchMatches = [];
  let searchIndex = -1;

  root.classList.toggle("is-timeline", layout === "timeline");
  canvas.hidden = layout === "timeline";
  timeline.hidden = layout !== "timeline";

  function emit(type, payload={}) {
    setTriggerValue("graph_event", {
      request_id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      type, ...payload
    });
    hideMenu();
  }

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
  }

  function hideMenu() { menu.hidden = true; menu.replaceChildren(); }

  function applyTransform() {
    activeStage.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  }

  function resetTransform() {
    scale = 1;
    tx = 0;
    ty = 0;
    applyTransform();
  }

  function zoomBy(delta) {
    const next = Math.max(0.55, Math.min(2.2, scale + delta));
    const rect = viewport.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const ratio = next / scale;
    tx = cx - (cx - tx) * ratio;
    ty = cy - (cy - ty) * ratio;
    scale = next;
    applyTransform();
  }

  function centerNode(node) {
    if (!node) return;
    const viewportRect = viewport.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    tx += viewportRect.left + viewportRect.width / 2
      - (nodeRect.left + nodeRect.width / 2);
    ty += viewportRect.top + viewportRect.height / 2
      - (nodeRect.top + nodeRect.height / 2);
    applyTransform();
  }

  function showSuccessNotice(value) {
    if (!value || !value.id || root.dataset.lastSuccess === String(value.id)) return;
    root.dataset.lastSuccess = String(value.id);
    notice.replaceChildren();
    const text = document.createElement("span");
    text.textContent = value.message || "操作成功";
    notice.appendChild(text);
    if (value.action_label) {
      const action = document.createElement("button");
      action.type = "button";
      action.textContent = value.action_label;
      action.addEventListener("click", () => emit("navigate", {
        target: value.target || "",
        section: value.section || "",
        draft_id: value.draft_id || "",
      }));
      notice.appendChild(action);
    }
    notice.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice.hidden = true; }, 3000);
  }

  function menuButton(label, cls, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (cls) button.className = cls;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      onClick();
    });
    menu.appendChild(button);
  }

  function showNodeMenu(x, y, item) {
    hideMenu();
    if (item.recommended) {
      menuButton("纳入引用图谱", "", () =>
        emit("adopt_recommendation", {openalex_id: item.openalex_id})
      );
      menuButton("忽略", "danger", () =>
        emit("ignore_recommendation", {openalex_id: item.openalex_id})
      );
      const recRect = root.getBoundingClientRect();
      menu.style.left = Math.max(4, Math.min(x - recRect.left, root.clientWidth - 200)) + "px";
      menu.style.top = Math.max(4, y - recRect.top) + "px";
      menu.hidden = false;
      return;
    }
    menuButton("打开文献", "", () => emit(
      item.paper_in_library ? "open_paper" : "open_candidate",
      {paper_id: item.paper_id}
    ));
    menuButton(item.is_focus ? "取消重点" : "标记为重点", "", () =>
      emit("toggle_focus", {relation_id: item.relation_id})
    );
    if (item.status === "rejected") {
      menuButton("恢复关系", "", () => emit("restore_relation", {relation_id: item.relation_id}));
    } else {
      menuButton("编辑备注", "", () => emit("edit_note", {relation_id: item.relation_id}));
      if (item.version_editable) {
        menuButton(item.version_hidden ? "恢复显示" : "在此版本隐藏", "", () =>
          emit(item.version_hidden ? "show_node" : "hide_node", {
            relation_id: item.relation_id
          })
        );
        menuButton("上移", "", () => emit("move_order", {
          relation_id: item.relation_id, delta: -1
        }));
        menuButton("下移", "", () => emit("move_order", {
          relation_id: item.relation_id, delta: 1
        }));
      }
      let armed = false;
      menuButton("从图谱移除", "danger", () => {
        if (!armed) {
          armed = true;
          [...menu.querySelectorAll("button")].find(
            (button) => button.classList.contains("danger")
          ).textContent = "确认驳回该引用关系";
          [...menu.querySelectorAll("button")].find(
            (button) => button.classList.contains("danger")
          ).classList.add("confirm");
          return;
        }
        emit("remove_relation", {relation_id: item.relation_id});
      });
    }
    if (!item.paper_in_library) {
      menuButton("导入文献库", "", () => emit("import_candidate", {paper_id: item.paper_id}));
    }
    const rect = root.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x - rect.left, root.clientWidth - 200)) + "px";
    menu.style.top = Math.max(4, y - rect.top) + "px";
    menu.hidden = false;
  }

  function showBlankMenu(x, y) {
    hideMenu();
    menuButton("添加项目内文献", "", () => emit("add_relation"));
    const rect = root.getBoundingClientRect();
    menu.style.left = Math.max(4, Math.min(x - rect.left, root.clientWidth - 200)) + "px";
    menu.style.top = Math.max(4, y - rect.top) + "px";
    menu.hidden = false;
  }

  function makeNode(item, timelineMode = false) {
    const node = document.createElement("article");
    node.className = `node ${item.status || ""}`;
    if (item.is_focus) node.classList.add("is-focus");
    if (item.is_selected) node.classList.add("is-selected");
    node.tabIndex = 0;
    node.draggable = !timelineMode && !item.recommended;
    node.dataset.relationId = item.relation_id;
    node.dataset.paperId = item.paper_id;
    node.dataset.searchable = `${item.title || ""} ${item.authors || ""}`.toLowerCase();
    node.dataset.allowedGroups = JSON.stringify(item.allowed_groups || []);
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", `${item.title}，${item.status_label}，点击联动引用列表`);

    const top = document.createElement("div"); top.className = "node__top";
    const title = document.createElement("span"); title.className = "node__title"; title.textContent = item.title || "未命名文献";
    const year = document.createElement("span"); year.className = "node__year"; year.textContent = item.year || "年份未知";
    top.append(title, year);
    node.appendChild(top);

    if (item.note) {
      const note = document.createElement("div"); note.className = "node__note"; note.textContent = item.note;
      node.appendChild(note);
    }
    const badge = document.createElement("span"); badge.className = "node__badge";
    badge.textContent = item.status_label || "关系";
    node.appendChild(badge);
    if (item.recommended) {
      const corner = document.createElement("span"); corner.className = "node__badge";
      corner.textContent = `推荐 · ${item.source_label || (item.sources || []).join(" + ")}`;
      node.appendChild(corner);
    }

    const tool = document.createElement("div"); tool.className = "tool";
    const toolTitle = document.createElement("b"); toolTitle.textContent = item.title || "未命名文献";
    tool.appendChild(toolTitle);
    tool.append(`${item.year || "年份未知"} · ${item.relation_label} · ${item.status_label}`);
    if (item.authors) {
      const authors = document.createElement("div"); authors.textContent = `作者：${item.authors}`;
      tool.appendChild(authors);
    }
    if (item.recommended) {
      const source = document.createElement("div");
      source.textContent = `来源：${item.source_label || (item.sources || []).join(" + ")}（算法推荐，待核验）`;
      tool.appendChild(source);
    }
    if (item.note) {
      const fullNote = document.createElement("div"); fullNote.textContent = `备注：${item.note}`;
      tool.appendChild(fullNote);
    }
    node.appendChild(tool);

    node.addEventListener("click", (event) => {
      if (event.detail > 1) return;
      if (item.recommended) {
        document.querySelectorAll(".node.is-selected").forEach((entry) => entry.classList.remove("is-selected"));
        node.classList.add("is-selected");
        showToast("右键推荐节点可纳入图谱或忽略");
      } else {
        emit("select_relation", {relation_id: item.relation_id});
      }
    });
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        if (item.recommended) {
          showNodeMenu(
            node.getBoundingClientRect().left + 16,
            node.getBoundingClientRect().top + 20,
            item,
          );
        } else {
          emit("select_relation", {relation_id: item.relation_id});
        }
        event.preventDefault();
      } else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        const rect = node.getBoundingClientRect();
        showNodeMenu(rect.left + 16, rect.top + 20, item);
        event.preventDefault();
      }
    });
    node.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      event.stopPropagation();
      showNodeMenu(event.clientX, event.clientY, item);
    });
    node.addEventListener("dragstart", (event) => {
      if (item.recommended) {
        event.preventDefault();
        return;
      }
      dragItem = item;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", item.relation_id);
      node.classList.add("is-dragging");
    });
    node.addEventListener("dragend", () => {
      dragItem = null;
      node.classList.remove("is-dragging");
      groups.forEach((group) => group.classList.remove("is-over"));
    });
    return node;
  }

  function fillCurrentBox() {
    currentBox.innerHTML = "";
    const currentLabel = document.createElement("small"); currentLabel.textContent = `${current.year || "年份未知"} · 当前论文`;
    const currentTitle = document.createElement("b"); currentTitle.textContent = current.title || "当前论文";
    currentBox.append(currentLabel, currentTitle);
  }

  groups.forEach((group) => group.querySelector(".cg__nodes").replaceChildren());
  fillCurrentBox();
  for (const item of allItems) {
    const groupName = item.graph_group || (item.side === "right" ? "later_extension" : "method_source");
    const group = groups.find((candidate) => candidate.dataset.group === groupName)
      || groups.find((candidate) => candidate.dataset.group === "related");
    if (group) group.querySelector(".cg__nodes").appendChild(makeNode(item, false));
  }

  groups.forEach((group) => {
    group.addEventListener("dragover", (event) => {
      if (!dragItem) return;
      const allowed = Array.isArray(dragItem.allowed_groups) ? dragItem.allowed_groups : [];
      if (!allowed.includes(group.dataset.group)) return;
      event.preventDefault();
      group.classList.add("is-over");
    });
    group.addEventListener("dragleave", () => group.classList.remove("is-over"));
    group.addEventListener("drop", (event) => {
      event.preventDefault();
      group.classList.remove("is-over");
      if (!dragItem) return;
      const target = group.dataset.group;
      const allowed = Array.isArray(dragItem.allowed_groups) ? dragItem.allowed_groups : [];
      if (!allowed.includes(target)) {
        showToast("引用方向不允许移到这个分组");
        return;
      }
      emit("move_group", {relation_id: dragItem.relation_id, graph_group: target});
    });
  });

  function drawTimeline() {
    timelineSvg.querySelectorAll("line").forEach((line) => line.remove());
    timelineNodes.replaceChildren();
    const items = [{
      relation_id: `current:${current.id}`,
      paper_id: current.id,
      title: current.title || "当前论文",
      authors: current.authors || "",
      year: current.year || "",
      graph_group: "current",
      status: "current",
      status_label: "当前论文",
      relation_label: "当前论文",
      is_selected: false,
      current: true,
    }, ...allItems];
    const years = items
      .map((item) => Number.parseInt(item.year, 10))
      .filter((year) => Number.isFinite(year));
    const minYear = years.length ? Math.min(...years) : new Date().getFullYear();
    const maxYear = years.length ? Math.max(...years) : minYear;
    const span = Math.max(1, maxYear - minYear);
    const laneY = {method_source: 72, current: 225, later_extension: 372, related: 500};
    const lanes = {method_source: 0, current: 0, later_extension: 0, related: 0};
    const positions = new Map();
    for (const item of items) {
      const parsed = Number.parseInt(item.year, 10);
      const year = Number.isFinite(parsed) ? parsed : minYear;
      const x = 88 + ((year - minYear) / span) * 780;
      const lane = laneY[item.graph_group] ?? laneY.related;
      const offset = lanes[item.graph_group] ?? 0;
      lanes[item.graph_group] = offset + 1;
      const y = lane + (offset % 3) * 62;
      positions.set(item.relation_id, {x, y});
    }
    for (const item of allItems.filter((entry) => !entry.recommended)) {
      const from = positions.get(`current:${current.id}`);
      const to = positions.get(item.relation_id);
      if (!from || !to) continue;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      const outward = item.edge_from_current !== false;
      line.setAttribute("x1", String(outward ? from.x + 105 : to.x + 105));
      line.setAttribute("y1", String(outward ? from.y + 29 : to.y + 29));
      line.setAttribute("x2", String(outward ? to.x + (to.x < from.x ? 105 : 0) : from.x + (from.x < to.x ? 105 : 0)));
      line.setAttribute("y2", String(outward ? to.y + 29 : from.y + 29));
      line.setAttribute("marker-end", "url(#cg-arrow)");
      timelineSvg.appendChild(line);
    }
    for (const item of items) {
      const position = positions.get(item.relation_id);
      const node = makeNode(item, true);
      if (item.current) node.classList.add("current");
      node.style.left = `${position.x}px`;
      node.style.top = `${position.y}px`;
      timelineNodes.appendChild(node);
    }
  }

  function applySearch(term) {
    const needle = String(term || "").trim().toLowerCase();
    searchMatches = needle
      ? allItems.filter((item) =>
          `${item.title || ""} ${item.authors || ""}`.toLowerCase().includes(needle)
        )
      : [];
    searchIndex = searchMatches.length ? 0 : -1;
    root.querySelectorAll(".node").forEach((node) => {
      node.classList.remove(
        "is-search-match", "is-search-current", "is-search-muted"
      );
      if (!needle) return;
      const matched = searchMatches.some((item) =>
        item.relation_id === node.dataset.relationId
      );
      node.classList.toggle("is-search-match", matched);
      node.classList.toggle("is-search-current",
        matched && searchMatches[searchIndex]?.relation_id === node.dataset.relationId
      );
      node.classList.toggle("is-search-muted", !matched);
    });
    searchCount.textContent = searchMatches.length
      ? `${searchIndex + 1} / ${searchMatches.length}`
      : "0 / 0";
  }

  function nextSearchResult() {
    if (!searchMatches.length) return;
    searchIndex = (searchIndex + 1) % searchMatches.length;
    const relationId = searchMatches[searchIndex].relation_id;
    root.querySelectorAll(".node").forEach((node) => {
      const currentMatch = node.dataset.relationId === relationId;
      node.classList.toggle("is-search-current", currentMatch);
      if (currentMatch) centerNode(node);
    });
    searchCount.textContent = `${searchIndex + 1} / ${searchMatches.length}`;
  }

  search.addEventListener("input", () => applySearch(search.value));
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (searchIndex < 0) applySearch(search.value);
      else nextSearchResult();
    }
    if (event.key === "Escape") {
      search.value = "";
      applySearch("");
      search.blur();
    }
  });

  recommendationList.replaceChildren();
  recommendationsBox.hidden = recommendationNodes.length === 0;
  recommendationsBox.open = recommendationNodes.length > 0;
  recommendationsBox.querySelector("summary").textContent =
    `拓展推荐 · 算法推荐待核验（${recommendationNodes.length}）`;
  for (const item of recommendationNodes) {
    const row = document.createElement("div");
    row.className = "cg__recommendation";
    const label = document.createElement("span");
    label.textContent = `${item.title || "未命名文献"} · ${item.source_label || (item.sources || []).join(" + ")}`;
    row.appendChild(label);
    const adopt = document.createElement("button");
    adopt.type = "button"; adopt.textContent = "纳入";
    adopt.addEventListener("click", () => emit(
      "adopt_recommendation", {openalex_id: item.openalex_id}
    ));
    const ignore = document.createElement("button");
    ignore.type = "button"; ignore.textContent = "忽略";
    ignore.addEventListener("click", () => emit(
      "ignore_recommendation", {openalex_id: item.openalex_id}
    ));
    row.append(adopt, ignore);
    recommendationList.appendChild(row);
  }

  const exportRequest = config.export_request || {};
  if (exportRequest.id && root.dataset.lastExport !== String(exportRequest.id)) {
    root.dataset.lastExport = String(exportRequest.id);
    const options = exportRequest.options || {};
    const scale = 2;
    const width = 1200;
    const columns = ["method_source", "later_extension", "related"];
    const maxRows = Math.max(3, ...columns.map((group) => nodes.filter(
      (item) => (item.graph_group || (item.side === "right" ? "later_extension" : "method_source")) === group
    ).length));
    const height = Math.max(620, 170 + maxRows * 78);
    const exportCanvas = document.createElement("canvas");
    exportCanvas.width = width * scale;
    exportCanvas.height = height * scale;
    const ctx = exportCanvas.getContext("2d");
    ctx.scale(scale, scale);
    ctx.fillStyle = "#FFFFFF"; ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = "#25312C"; ctx.font = "600 20px sans-serif";
    ctx.fillText(`引用关系图谱 · ${current.title || "当前论文"}`, 34, 42);
    ctx.fillStyle = "#748079"; ctx.font = "12px sans-serif";
    ctx.fillText(`${current.year || "年份未知"} · ${nodes.length} 条关系`, 34, 64);

    function wrap(text, maxWidth) {
      const output = []; let line = "";
      for (const char of String(text || "")) {
        if (ctx.measureText(line + char).width > maxWidth && line) {
          output.push(line); line = char;
        } else { line += char; }
        if (output.length >= 2) break;
      }
      if (line && output.length < 2) output.push(line);
      return output;
    }

    const centerX = 465, centerY = 88, centerW = 270, centerH = 72;
    ctx.fillStyle = "#173F3B";
    ctx.beginPath(); ctx.roundRect(centerX, centerY, centerW, centerH, 12); ctx.fill();
    ctx.fillStyle = "#FFFFFF"; ctx.font = "600 14px sans-serif";
    wrap(current.title || "当前论文", 238).forEach((line, index) =>
      ctx.fillText(line, centerX + 16, centerY + 28 + index * 18)
    );
    ctx.fillStyle = "#D8EBE7"; ctx.font = "11px sans-serif";
    ctx.fillText(`${current.year || "年份未知"} · 当前论文`, centerX + 16, centerY + 58);

    const grouped = Object.fromEntries(columns.map((group) => [
      group, nodes.filter((item) =>
        (item.graph_group || (item.side === "right" ? "later_extension" : "method_source")) === group
      )
    ]));
    function drawColumn(group, x, yStart, title) {
      ctx.fillStyle = "#66736D"; ctx.font = "600 12px sans-serif";
      ctx.fillText(title, x, yStart - 14);
      grouped[group].forEach((item, index) => {
        const y = yStart + index * 78;
        ctx.strokeStyle = "#AEBAB4"; ctx.lineWidth = 1.2;
        ctx.beginPath();
        if (x < centerX) {
          ctx.moveTo(x + 290, y + 26); ctx.lineTo(centerX, centerY + centerH / 2);
        } else {
          ctx.moveTo(centerX + centerW, centerY + centerH / 2); ctx.lineTo(x, y + 26);
        }
        ctx.stroke();
        ctx.fillStyle = item.status === "official" ? "#1F5F5B"
          : item.status === "pending" ? "#FFF8EA"
          : item.status === "rejected" ? "#F3F5F4" : "#F8FAF9";
        ctx.strokeStyle = item.status === "official" ? "#1F5F5B" : "#C9D2CD";
        ctx.lineWidth = item.status === "manual" ? 2 : 1;
        ctx.beginPath(); ctx.roundRect(x, y, 290, 52, 8); ctx.fill(); ctx.stroke();
        ctx.fillStyle = item.status === "official" ? "#FFFFFF" : "#27332D";
        ctx.font = "600 11px sans-serif";
        wrap(item.title, 250).forEach((line, lineIndex) =>
          ctx.fillText(line, x + 10, y + 18 + lineIndex * 14)
        );
        let meta = `${item.year || "年份未知"}`;
        if (!options.hide_status) meta += ` · ${item.status_label}`;
        if (options.include_notes && item.note) meta += ` · ${item.note}`;
        ctx.fillStyle = item.status === "official" ? "#D8EBE7" : "#77837D";
        ctx.font = "9px sans-serif"; ctx.fillText(meta, x + 10, y + 45);
      });
    }
    drawColumn("method_source", 34, 118, "方法来源 / 前期工作");
    drawColumn("later_extension", 876, 118, "后续延伸");
    const relatedY = 118 + maxRows * 78;
    ctx.fillStyle = "#66736D"; ctx.font = "600 12px sans-serif";
    ctx.fillText("相关研究", 34, relatedY - 14);
    grouped.related.slice(0, 6).forEach((item, index) => {
      const x = 34 + index * 190, y = relatedY;
      ctx.fillStyle = "#F8FAF9"; ctx.strokeStyle = "#C9D2CD";
      ctx.beginPath(); ctx.roundRect(x, y, 178, 48, 7); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#27332D"; ctx.font = "600 10px sans-serif";
      wrap(item.title, 154).forEach((line, lineIndex) =>
        ctx.fillText(line, x + 8, y + 17 + lineIndex * 13)
      );
    });
    const link = document.createElement("a");
    link.download = `citation-graph-${current.id || "paper"}.png`;
    link.href = exportCanvas.toDataURL("image/png");
    link.click();
    setTriggerValue("graph_event", {
      request_id: `${Date.now()}-export`,
      type: "export_done",
    });
  }

  const blankContext = (event) => {
    if (event.target.closest?.(".node")) return;
    event.preventDefault();
    showBlankMenu(event.clientX, event.clientY);
  };
  canvas.addEventListener("contextmenu", blankContext);
  timeline.addEventListener("contextmenu", blankContext);

  viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomBy(event.deltaY < 0 ? 0.1 : -0.1);
  }, {passive: false});
  viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest?.(".node, .cg__zoom, .tool")) return;
    panning = true;
    panStart = {x: event.clientX, y: event.clientY, tx, ty};
    viewport.style.cursor = "grabbing";
  });
  window.addEventListener("pointermove", (event) => {
    if (!panning || !panStart) return;
    tx = panStart.tx + event.clientX - panStart.x;
    ty = panStart.ty + event.clientY - panStart.y;
    applyTransform();
  });
  window.addEventListener("pointerup", () => {
    panning = false;
    panStart = null;
    viewport.style.cursor = "";
  });
  root.querySelectorAll("[data-zoom]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.zoom === "in") zoomBy(0.15);
      else if (button.dataset.zoom === "out") zoomBy(-0.15);
      else resetTransform();
    });
  });
  document.addEventListener("click", hideMenu);

  drawTimeline();
  applyTransform();
  showSuccessNotice(config.success_notice);
  if (config.search_query) {
    search.value = String(config.search_query);
    applySearch(search.value);
  }
  const selected = root.querySelector(
    `.node.is-selected[data-relation-id="${CSS.escape(String(config.selected_relation_id || ""))}"]`
  );
  if (selected) setTimeout(() => centerNode(selected), 0);
  return () => {
    clearTimeout(toastTimer);
    clearTimeout(noticeTimer);
    document.removeEventListener("click", hideMenu);
  };
}
"""


def _component():
    return st.components.v2.component(
        "rp_citation_graph_workspace",
        html=_HTML,
        css=_CSS,
        js=_JS,
    )


_SCROLL_JS = """
export default function(component) {
  const {data, parentElement} = component;
  const config = data && typeof data === "object" ? data : {};
  const doc = parentElement?.ownerDocument || document;
  const selector = `.st-key-rp_relation_card_${config.paper_id}_${config.relation_id}`;
  let attempts = 0;
  const locate = () => {
    const target = doc.querySelector(selector);
    if (target) {
      target.scrollIntoView({behavior: "smooth", block: "center"});
      return;
    }
    if (attempts < 20) {
      attempts += 1;
      setTimeout(locate, 100);
    }
  };
  setTimeout(locate, 0);
}
"""


def scroll_to_relation(paper_id: str, relation_id: str) -> None:
    """Scroll the rendered citation card into view after a graph selection."""
    if not paper_id or not relation_id:
        return
    try:
        st.components.v2.component("rp_relation_scroll", js=_SCROLL_JS)(
            key=f"rp_relation_scroll_{paper_id}_{relation_id}",
            data={"paper_id": paper_id, "relation_id": relation_id},
            default={},
            height=0,
        )
    except Exception:  # noqa: BLE001 - linkage still works if scrolling is unavailable
        return


def _decode(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _recommendation_key(paper_id: str) -> str:
    return f"cite_graph_recommendations_{paper_id}"


def recommendation_items(paper: Paper) -> list[dict[str, Any]]:
    raw = st.session_state.get(_recommendation_key(paper.id))
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        value = dict(item)
        sources = [str(source) for source in value.get("sources") or []]
        value["sources"] = sources
        value["source_label"] = " + ".join(dict.fromkeys(sources))
        result.append(value)
    return result


def _save_recommendation_items(paper: Paper, items: list[dict[str, Any]]) -> None:
    st.session_state[_recommendation_key(paper.id)] = [
        dict(item) for item in items if isinstance(item, dict)
    ]


def _adopt_recommendation(
    project: Project,
    current: Paper,
    openalex_id: str,
) -> str:
    items = recommendation_items(current)
    selected = next(
        (item for item in items if item.get("openalex_id") == openalex_id),
        None,
    )
    if selected is None:
        return "推荐已失效，请重新拓展"
    recommendation = graph_recommendations.GraphRecommendation.from_dict(selected)
    candidate, _ = cite_mod.import_candidate_paper(
        project.id, recommendation.as_work()
    )
    library.adopt_candidate(project.id, candidate.id)
    sources = " + ".join(recommendation.sources) or "算法推荐"
    relation = PaperRelation(
        id=repo.new_id("prel"),
        project_id=project.id,
        from_paper_id=current.id,
        to_paper_id=candidate.id,
        relation_type=RelationType.COMPARES,
        source=RelationSource.USER,
        locator=f"人工确认自 OpenAlex {sources}（算法推荐，待核验）",
        certainty=Certainty.STATED,
        verified=True,
        review_status=ReviewStatus.CONFIRMED,
        review_note=f"算法推荐来源：{sources}；已由用户人工确认纳入",
        graph_group="related",
    )
    saved, _ = repo.add_paper_relation(relation)
    saved.graph_group = "related"
    saved.review_note = relation.review_note
    repo.save(saved)
    _save_recommendation_items(
        current,
        [item for item in items if item.get("openalex_id") != openalex_id],
    )
    return f"已纳入引用图谱：{candidate.title or recommendation.title}"


def _ignore_recommendation(current: Paper, openalex_id: str) -> str:
    items = recommendation_items(current)
    _save_recommendation_items(
        current,
        [item for item in items if item.get("openalex_id") != openalex_id],
    )
    return "已忽略这条推荐，未写入引用数据"


def _relation_paper(relation: PaperRelation, current: Paper) -> tuple[Paper | None, str]:
    if relation.from_paper_id == current.id:
        return repo.get(Paper, relation.to_paper_id), "left"
    return repo.get(Paper, relation.from_paper_id), "right"


def _default_group(relation: PaperRelation, current: Paper) -> str:
    if relation.graph_group:
        return relation.graph_group
    if relation.relation_type is not RelationType.CITES:
        return "related"
    return "method_source" if relation.from_paper_id == current.id else "later_extension"


def _status(relation: PaperRelation, paper: Paper | None) -> tuple[str, str]:
    if relation.review_status is ReviewStatus.REJECTED:
        return "rejected", "已驳回"
    if paper is None or paper.origin == "openalex":
        return "candidate", "候选"
    if relation.source is RelationSource.OPENALEX:
        return "official", "官方引用 · 已核实"
    if relation.verified and relation.review_status is ReviewStatus.CONFIRMED:
        return "manual", "人工增补 · 人工确认"
    return "pending", "待人工判断"


def _allowed_groups(relation: PaperRelation, current: Paper) -> list[str]:
    if relation.from_paper_id == current.id:
        return ["method_source", "related"]
    if relation.to_paper_id == current.id:
        return ["later_extension", "related"]
    return ["related"]


def _relation_label(relation: PaperRelation, group: str) -> str:
    labels = {
        "method_source": "方法来源 / 前期工作",
        "later_extension": "后续延伸",
        "related": "相关研究",
    }
    if group in labels:
        return labels[group]
    return {
        RelationType.CITES: "引用",
        RelationType.EXTENDS: "方法扩展",
        RelationType.COMPARES: "比较对照",
        RelationType.REPLACES: "替代",
        RelationType.DIVERGES: "不同方法",
    }.get(relation.relation_type, "关系")


def _payload(
    project: Project,
    current: Paper,
    relations: list[PaperRelation],
    *,
    show_rejected: bool,
    version_id: str = graph_versions.AUTO_VERSION,
    version_item: dict[str, Any] | None = None,
    export_request: dict[str, Any] | None = None,
    show_hidden: bool = False,
    recommendations: list[dict[str, Any]] | None = None,
    selected_relation_id: str = "",
    layout: str = "radial",
    search_query: str = "",
    success_notice: dict[str, Any] | None = None,
) -> dict:
    raw_nodes: list[tuple[int, int, int, dict]] = []
    configs = (version_item or {}).get("nodes") or {}
    focused = set(graph_preferences.focused_ids(current))
    for relation in relations:
        config = configs.get(relation.id) if isinstance(configs, dict) else None
        config = config if isinstance(config, dict) else {}
        version_hidden = config.get("visible") is False
        if version_hidden and not show_hidden:
            continue
        if relation.review_status is ReviewStatus.REJECTED and not show_rejected:
            continue
        other, side = _relation_paper(relation, current)
        if other is None:
            continue
        group = str(config.get("graph_group") or _default_group(relation, current))
        if group not in {"method_source", "later_extension", "related"}:
            group = _default_group(relation, current)
        status, status_label = _status(relation, other)
        version_new = bool(version_item) and relation.id not in configs
        if version_new:
            status = "pending"
            status_label = "版本新增 · 待纳入"
        elif version_hidden:
            status = "pending"
            status_label = "版本内隐藏"
        note = (
            str(config.get("note"))
            if "note" in config
            else relation.review_note
        )[:120]
        is_focus = relation.id in focused
        raw_nodes.append((
            0 if is_focus else 1,
            int(config.get("sort", 10**9)),
            len(raw_nodes),
            {
                "relation_id": relation.id,
                "paper_id": other.id,
                "title": other.title or "未命名文献",
                "authors": other.authors or "",
                "year": other.year or "",
                "note": note,
                "status": status,
                "status_label": status_label,
                "relation_label": _relation_label(relation, group),
                "graph_group": group,
                "side": side,
                "paper_in_library": other.origin != "openalex",
                "allowed_groups": _allowed_groups(relation, current),
                "version_editable": version_id != graph_versions.AUTO_VERSION,
                "version_new": version_new,
                "version_hidden": version_hidden,
                "is_focus": is_focus,
                "is_selected": relation.id == selected_relation_id,
                "edge_from_current": relation.from_paper_id == current.id,
            },
        ))
    raw_nodes.sort(key=lambda item: (item[0], item[1], item[2]))
    nodes = [item[3] for item in raw_nodes]
    return {
        "project_id": project.id,
        "version_id": version_id,
        "layout": layout if layout in {"radial", "timeline"} else "radial",
        "search_query": search_query,
        "selected_relation_id": selected_relation_id,
        "current": {
            "id": current.id,
            "title": current.title or "当前论文",
            "authors": current.authors or "",
            "year": current.year or "",
        },
        "nodes": nodes,
        "recommendations": recommendations or [],
        "show_rejected": show_rejected,
        "export_request": export_request or {},
        "success_notice": success_notice or {},
    }


def _apply_action(
    project: Project,
    current: Paper,
    widget_key: str,
    version_id: str,
) -> None:
    component_state = st.session_state.get(widget_key)
    if isinstance(component_state, dict):
        raw_action = component_state.get("graph_event")
    else:
        raw_action = getattr(component_state, "graph_event", None)
    action = _decode(raw_action)
    request_id = str(action.get("request_id") or "")
    seen_key = f"_citation_graph_seen_{current.id}"
    seen = st.session_state.get(seen_key)
    if not isinstance(seen, set):
        seen = set()
    if not action or request_id in seen:
        return
    seen.add(request_id)
    st.session_state[seen_key] = seen
    kind = str(action.get("type") or "")

    if kind == "navigate":
        st.session_state["_graph_navigation"] = {
            "target": str(action.get("target") or ""),
            "section": str(action.get("section") or ""),
            "draft_id": str(action.get("draft_id") or ""),
            "project_id": project.id,
        }
        st.rerun()

    if kind in {"adopt_recommendation", "ignore_recommendation"}:
        openalex_id = str(action.get("openalex_id") or "")
        if not openalex_id:
            return
        if kind == "adopt_recommendation":
            message = _adopt_recommendation(project, current, openalex_id)
        else:
            message = _ignore_recommendation(current, openalex_id)
        st.session_state["_library_notice"] = ("success", message)
        st.rerun()

    if kind == "export_done":
        st.session_state.pop(f"cite_graph_export_request_{current.id}", None)
        return

    if kind in {"open_paper", "open_candidate", "import_candidate"}:
        paper_id = str(action.get("paper_id") or "")
        paper = repo.get(Paper, paper_id)
        if paper is None or paper.project_id != project.id:
            return
        if kind == "open_paper":
            st.session_state["selected_paper_id"] = paper.id
            st.session_state["selected_paper_project"] = project.id
            st.session_state["_detail_default_tab"] = "引用"
        else:
            st.session_state[f"cite_graph_candidate_{current.id}"] = paper.id
        st.rerun()

    if kind == "add_relation":
        st.session_state[f"cite_graph_add_{current.id}"] = True
        st.rerun()

    relation_id = str(action.get("relation_id") or "")
    relation = repo.get(PaperRelation, relation_id)
    if (
        relation is None
        or relation.project_id != project.id
        or current.id not in (relation.from_paper_id, relation.to_paper_id)
    ):
        return
    if kind == "select_relation":
        graph_preferences.select_relation(current, relation.id)
        st.session_state[f"citation_scroll_relation_{current.id}"] = relation.id
        st.session_state["_detail_default_tab"] = "引用"
        st.rerun()
    elif kind == "toggle_focus":
        _focused, message = graph_preferences.toggle_focus(current, relation)
        st.session_state["_library_notice"] = (
            "success" if _focused else "info",
            message,
        )
        st.rerun()
    elif kind == "edit_note":
        st.session_state[f"cite_graph_note_{current.id}"] = relation.id
        st.rerun()
    elif kind == "remove_relation":
        repo.review_paper_relation(
            relation, ReviewStatus.REJECTED, relation.review_note
        )
        st.rerun()
    elif kind == "restore_relation":
        repo.review_paper_relation(
            relation, ReviewStatus.PENDING, relation.review_note
        )
        st.rerun()
    elif kind == "move_group":
        group = str(action.get("graph_group") or "")
        if group not in _allowed_groups(relation, current):
            return
        if version_id == graph_versions.AUTO_VERSION:
            relation.graph_group = group
            repo.save(relation)
        else:
            graph_versions.update_node(
                current, version_id, relation.id, graph_group=group
            )
        st.rerun()
    elif kind in {"hide_node", "show_node"}:
        if version_id == graph_versions.AUTO_VERSION:
            return
        graph_versions.update_node(
            current,
            version_id,
            relation.id,
            visible=kind == "show_node",
        )
        st.rerun()
    elif kind == "move_order":
        if version_id == graph_versions.AUTO_VERSION:
            return
        graph_versions.move_order(
            current,
            version_id,
            relation.id,
            int(action.get("delta") or 0),
            repo.list_paper_relations(
                project.id, paper_id=current.id, direction="both"
            ),
        )
        st.rerun()


@st.dialog("候选文献")
def _candidate_dialog(project: Project, paper: Paper, state_key: str) -> None:
    st.markdown(f"**{paper.title or '未命名文献'}**")
    st.caption(f"{paper.year or '年份未知'} · {paper.venue or '来源未知'} · OpenAlex 候选")
    if st.button("导入文献库", type="primary", width="stretch"):
        _, changed = library.adopt_candidate(project.id, paper.id)
        st.session_state.pop(state_key, None)
        st.session_state["_library_notice"] = (
            "success" if changed else "info",
            "已导入当前项目文献库" if changed else "这篇文献已经在文献库中",
        )
        st.rerun()


@st.dialog("编辑节点备注")
def _note_dialog(
    current: Paper,
    relation: PaperRelation,
    state_key: str,
    version_id: str,
) -> None:
    initial = relation.review_note
    if version_id != graph_versions.AUTO_VERSION:
        config = graph_versions.node_config(current, version_id, relation.id)
        if "note" in config:
            initial = str(config.get("note") or "")
    note = st.text_input(
        "备注",
        value=initial,
        placeholder="例如：核心方法源头；可用于复现对照",
    )
    if st.button("保存备注", type="primary", width="stretch"):
        if version_id == graph_versions.AUTO_VERSION:
            relation.review_note = note.strip()[:500]
            repo.save(relation)
        else:
            graph_versions.update_node(
                current, version_id, relation.id, note=note.strip()[:500]
            )
        st.session_state.pop(state_key, None)
        st.rerun()


@st.dialog("添加项目内文献")
def _add_dialog(project: Project, current: Paper, state_key: str) -> None:
    connected = {
        relation.to_paper_id if relation.from_paper_id == current.id else relation.from_paper_id
        for relation in repo.list_paper_relations(
            project.id, paper_id=current.id, direction="both"
        )
    }
    choices = [
        paper for paper in repo.list_all(Paper, project.id)
        if paper.id != current.id and paper.id not in connected
    ]
    if not choices:
        st.caption("当前项目没有可添加的其他文献。")
        return
    labels = {
        paper.id: f"{paper.year or '????'} · {paper.title or '未命名文献'}"
        for paper in choices
    }
    paper_id = st.selectbox(
        "选择文献", list(labels), format_func=labels.get
    )
    group_label = st.selectbox(
        "图谱分组",
        ["方法来源", "后续延伸", "相关研究"],
    )
    locator = st.text_input(
        "证据位置（必填）",
        placeholder="例如：第 7 页 §4.2 或作者明确说明的方法继承关系",
    )
    note = st.text_input("备注（可选）", placeholder="例如：核心方法源头")
    if st.button("添加到图谱", type="primary", width="stretch", disabled=not locator.strip()):
        other = repo.get(Paper, paper_id)
        if other is None or other.project_id != project.id:
            st.error("所选文献不属于当前项目。")
            return
        if group_label == "方法来源":
            from_id, to_id = current.id, other.id
            relation_type, graph_group = RelationType.EXTENDS, "method_source"
        elif group_label == "后续延伸":
            from_id, to_id = other.id, current.id
            relation_type, graph_group = RelationType.EXTENDS, "later_extension"
        else:
            from_id, to_id = current.id, other.id
            relation_type, graph_group = RelationType.COMPARES, "related"
        relation = PaperRelation(
            id=repo.new_id("prel"),
            project_id=project.id,
            from_paper_id=from_id,
            to_paper_id=to_id,
            relation_type=relation_type,
            source=RelationSource.USER,
            locator=locator.strip(),
            certainty=Certainty.STATED,
            verified=False,
            review_status=ReviewStatus.PENDING,
            review_note=note.strip(),
            graph_group=graph_group,
        )
        saved, _created = repo.add_paper_relation(relation)
        saved.graph_group = graph_group
        saved.review_note = note.strip()
        repo.save(saved)
        repo.review_paper_relation(saved, ReviewStatus.CONFIRMED, note.strip())
        st.session_state.pop(state_key, None)
        st.session_state["_library_notice"] = ("success", "已人工增补到引用关系图谱")
        st.rerun()


def _effective_nodes(
    current: Paper,
    relations: list[PaperRelation],
    version_item: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    configs = (version_item or {}).get("nodes") or {}
    result: dict[str, dict[str, Any]] = {}
    for order, relation in enumerate(relations):
        config = configs.get(relation.id) if isinstance(configs, dict) else {}
        config = config if isinstance(config, dict) else {}
        result[relation.id] = {
            "visible": config.get("visible", True),
            "graph_group": config.get("graph_group") or _default_group(relation, current),
            "note": config.get("note", relation.review_note),
            "sort": int(config.get("sort", order)),
        }
    return result


def _execute_batch(
    project: Project,
    current: Paper,
    relations: list[PaperRelation],
    action: str,
    version_id: str,
) -> str:
    if action == "confirm_official":
        count = 0
        for relation in relations:
            if (
                relation.relation_type is RelationType.CITES
                and relation.source is RelationSource.OPENALEX
                and relation.verified
                and relation.review_status is not ReviewStatus.REJECTED
            ):
                repo.review_paper_relation(
                    relation, ReviewStatus.CONFIRMED, relation.review_note
                )
                count += 1
        return f"已确认 {count} 条官方引用"
    if action == "reject_low_confidence":
        count = 0
        for relation in relations:
            official = (
                relation.relation_type is RelationType.CITES
                and relation.source is RelationSource.OPENALEX
                and relation.verified
            )
            if not official and (
                relation.review_status is ReviewStatus.PENDING
                or not relation.verified
            ):
                repo.review_paper_relation(
                    relation, ReviewStatus.REJECTED, relation.review_note
                )
                count += 1
        return f"已驳回 {count} 条低置信关系，审计记录保留"
    if action == "clear_notes":
        count = 0
        for relation in relations:
            if relation.review_note:
                relation.review_note = ""
                repo.save(relation)
                count += 1
        count += graph_versions.clear_custom_notes(current)
        return f"已清空 {count} 条人工备注"
    if action == "toggle_rejected":
        return "已切换已驳回节点显隐"
    return "未执行"


@st.dialog("确认批量操作")
def _batch_dialog(
    project: Project,
    current: Paper,
    relations: list[PaperRelation],
    action: str,
    version_id: str,
    state_key: str,
) -> None:
    current_show = (
        bool(st.session_state.get(
            f"cite_graph_custom_show_rejected_{current.id}_{version_id}", False
        ))
        if version_id != graph_versions.AUTO_VERSION
        else bool(st.session_state.get(
            f"cite_graph_auto_show_rejected_{current.id}", False
        ))
    )
    labels = {
        "confirm_official": "将所有官方引用标记为已确认",
        "reject_low_confidence": "驳回所有低置信度候选关系",
        "clear_notes": "清空所有人工备注",
        "toggle_rejected": (
            "隐藏已驳回节点"
            if current_show
            else "显示已驳回节点"
        ),
    }
    st.markdown(f"**{labels.get(action, action)}**")
    if action == "confirm_official":
        st.caption("只确认带 OpenAlex 外部证据的引用；不会修改论文元数据或引用方向。")
    elif action == "reject_low_confidence":
        st.caption("只会驳回待人工判断或未核实的关系，不删除任何记录。")
    elif action == "clear_notes":
        st.caption("只清除备注文本，引用关系、状态、分组和论文元数据均保留。")
    else:
        st.caption("仅改变当前图谱视图的显示状态。")
    confirm = st.checkbox("我确认执行这项批量操作")
    if st.button(
        "确认执行",
        type="primary",
        width="stretch",
        disabled=not confirm,
    ):
        if action == "toggle_rejected":
            key = (
                f"cite_graph_custom_show_rejected_{current.id}_{version_id}"
                if version_id != graph_versions.AUTO_VERSION
                else f"cite_graph_auto_show_rejected_{current.id}"
            )
            value = not bool(st.session_state.get(key, False))
            st.session_state[key] = value
            if version_id != graph_versions.AUTO_VERSION:
                graph_versions.set_show_rejected(current, version_id, value)
            message = "已显示已驳回节点" if value else "已隐藏已驳回节点"
        else:
            message = _execute_batch(
                project, current, relations, action, version_id
            )
        st.session_state.pop(state_key, None)
        st.session_state["_library_notice"] = ("success", message)
        st.rerun()
    if st.button("取消", width="stretch"):
        st.session_state.pop(state_key, None)
        st.rerun()


@st.dialog("导出方法演进大纲")
def _outline_dialog(
    project: Project,
    current: Paper,
    relations: list[PaperRelation],
    version_id: str,
    state_key: str,
) -> None:
    outline = graph_exports.build_method_outline(
        project, current, relations, version_id
    )
    st.caption(
        f"共 {len(outline['items'])} 条方法节点，只包含已进入下游的真实引用。"
    )
    st.markdown(outline["markdown"][:3000])
    st.download_button(
        "下载 Markdown 大纲",
        data=outline["markdown"],
        file_name="方法演进大纲.md",
        mime="text/markdown",
        width="stretch",
    )
    if st.button("导入科研成果工作台", type="primary", width="stretch"):
        artifact = graph_exports.save_outline_artifact(project, outline)
        st.session_state.pop(state_key, None)
        st.session_state["_library_notice"] = (
            "success", "方法演进大纲已写入科研成果报告章节"
        )
        st.session_state[f"cite_graph_success_notice_{current.id}"] = {
            "id": artifact.id,
            "message": "方法演进大纲导出成功",
            "action_label": "前往科研成果查看",
            "target": "outcomes",
            "section": "method_outline",
        }
        st.rerun()
    if st.button("取消", width="stretch"):
        st.session_state.pop(state_key, None)
        st.rerun()


@st.dialog("生成复现方案对比")
def _comparison_dialog(
    project: Project,
    current: Paper,
    relations: list[PaperRelation],
    state_key: str,
) -> None:
    candidate_ids = {
        relation.to_paper_id if relation.from_paper_id == current.id
        else relation.from_paper_id
        for relation in relations
        if relation.review_status is not ReviewStatus.REJECTED
        and relation.downstream_ready
    }
    candidates = [
        paper for paper in repo.list_all(Paper, project.id)
        if paper.id in candidate_ids
    ]
    if len(candidates) < 2:
        st.caption("至少需要两篇已进入下游的关联文献。")
        if st.button("关闭", width="stretch"):
            st.session_state.pop(state_key, None)
            st.rerun()
        return
    labels = {
        paper.id: f"{paper.year or '????'} · {paper.title or '未命名方法'}"
        for paper in candidates
    }
    chosen = st.multiselect(
        "选择方法类文献",
        list(labels),
        default=list(labels)[:min(4, len(labels))],
        format_func=labels.get,
    )
    selected = [repo.get(Paper, paper_id) for paper_id in chosen]
    selected = [paper for paper in selected if paper is not None]
    if len(selected) >= 2:
        st.dataframe(
            graph_exports.comparison_rows(selected),
            hide_index=True,
            width="stretch",
        )
    if st.button(
        "导入复现规划",
        type="primary",
        width="stretch",
        disabled=len(selected) < 2,
    ):
        draft = graph_exports.create_comparison_draft(project, current, selected)
        st.session_state.pop(state_key, None)
        st.session_state["_library_notice"] = (
            "success", f"复现方案对比草稿已保存：{draft.series_title}"
        )
        st.session_state[f"cite_graph_success_notice_{current.id}"] = {
            "id": draft.id,
            "message": "复现方案对比导出成功",
            "action_label": "前往复现规划查看",
            "target": "planner",
            "draft_id": draft.id,
        }
        st.rerun()
    if st.button("取消", width="stretch"):
        st.session_state.pop(state_key, None)
        st.rerun()


@st.dialog("导出图谱为 PNG")
def _png_dialog(current: Paper, state_key: str) -> None:
    include_notes = st.checkbox("包含节点备注", value=True)
    hide_status = st.checkbox("隐藏状态标签", value=False)
    if st.button("生成并下载 PNG", type="primary", width="stretch"):
        st.session_state[f"cite_graph_export_request_{current.id}"] = {
            "id": repo.new_id("export"),
            "options": {
                "include_notes": bool(include_notes),
                "hide_status": bool(hide_status),
            },
        }
        st.session_state.pop(state_key, None)
        st.rerun()
    if st.button("取消", width="stretch"):
        st.session_state.pop(state_key, None)
        st.rerun()


def render(project: Project, current: Paper, relations: list[PaperRelation]) -> None:
    """Render the graph workspace and its server-side follow-up dialogs."""
    rejected_count = sum(
        1 for relation in relations
        if relation.review_status is ReviewStatus.REJECTED
    )
    custom_versions = graph_versions.list_custom(current)
    version_key = f"cite_graph_version_{current.id}"
    version_labels = {graph_versions.AUTO_VERSION: "系统自动版"}
    version_labels.update({
        item["id"]: str(item.get("name") or "未命名版本")
        for item in custom_versions
    })
    if st.session_state.get(version_key) not in version_labels:
        st.session_state[version_key] = graph_versions.AUTO_VERSION
    version_id = str(st.session_state[version_key])
    version_item = graph_versions.version(current, version_id)
    editable_version = version_item is not None
    hidden_key = f"cite_graph_show_hidden_{current.id}_{version_id}"
    show_hidden = bool(st.session_state.get(hidden_key, False))
    show_key = (
        f"cite_graph_custom_show_rejected_{current.id}_{version_id}"
        if editable_version
        else f"cite_graph_auto_show_rejected_{current.id}"
    )
    if editable_version and show_key not in st.session_state:
        st.session_state[show_key] = bool(version_item.get("show_rejected"))
    show_rejected = bool(st.session_state.get(show_key, False))

    layout_key = f"cite_graph_layout_{current.id}"
    if layout_key not in st.session_state:
        remembered = str(browser_prefs.get("citation_graph_layout", "radial"))
        st.session_state[layout_key] = (
            remembered if remembered in {"radial", "timeline"} else "radial"
        )
    toolbar = st.columns([2.5, 1.25, 1.05, 1.05, 1.05], vertical_alignment="bottom")
    with toolbar[0]:
        selected_version = st.selectbox(
            "图谱版本",
            list(version_labels),
            index=list(version_labels).index(version_id),
            format_func=version_labels.get,
            key=version_key,
        )
        if selected_version != version_id:
            version_id = selected_version
            version_item = graph_versions.version(current, version_id)
            editable_version = version_item is not None
            hidden_key = f"cite_graph_show_hidden_{current.id}_{version_id}"
            show_hidden = bool(st.session_state.get(hidden_key, False))
            show_key = (
                f"cite_graph_custom_show_rejected_{current.id}_{version_id}"
                if editable_version
                else f"cite_graph_auto_show_rejected_{current.id}"
            )
            if editable_version:
                st.session_state[show_key] = bool(version_item.get("show_rejected"))
            show_rejected = bool(st.session_state.get(show_key, False))
    with toolbar[1]:
        layout_labels = {
            "radial": "放射布局",
            "timeline": "时间轴布局",
        }
        selected_layout = st.selectbox(
            "图谱布局",
            list(layout_labels),
            format_func=layout_labels.get,
            key=layout_key,
        )
        if browser_prefs.get("citation_graph_layout") != selected_layout:
            browser_prefs.set(citation_graph_layout=selected_layout)
    with toolbar[2].popover("版本", width="stretch"):
        st.caption(
            f"自定义版本 {len(custom_versions)}/{graph_versions.MAX_CUSTOM_VERSIONS}"
        )
        version_name = st.text_input(
            "版本名称",
            key=f"cite_graph_version_name_{current.id}",
            placeholder="例如：综述主线版",
            disabled=len(custom_versions) >= graph_versions.MAX_CUSTOM_VERSIONS,
        )
        if st.button(
            "保存为新版本",
            key=f"cite_graph_version_save_{current.id}",
            width="stretch",
            disabled=(
                not version_name.strip()
                or len(custom_versions) >= graph_versions.MAX_CUSTOM_VERSIONS
            ),
        ):
            item = graph_versions.create(
                current,
                version_name,
                relations,
                show_rejected=show_rejected,
                nodes=_effective_nodes(current, relations, version_item),
            )
            st.session_state[version_key] = item["id"]
            st.session_state["_library_notice"] = ("success", f"已保存图谱版本「{item['name']}」")
            st.rerun()
        if len(custom_versions) >= graph_versions.MAX_CUSTOM_VERSIONS:
            st.caption("已达到 3 个版本上限，请先删除旧版本。")
        if editable_version:
            st.checkbox(
                "显示版本内隐藏节点",
                key=hidden_key,
                help="只影响当前自定义版本的显示，不改变底层引用关系",
            )
        st.divider()
        delete_ok = st.checkbox(
            "我确认删除当前自定义版本",
            key=f"cite_graph_version_delete_ok_{current.id}",
            disabled=not editable_version,
        )
        if st.button(
            "删除当前版本",
            key=f"cite_graph_version_delete_{current.id}",
            type="tertiary",
            width="stretch",
            disabled=not editable_version or not delete_ok,
        ):
            graph_versions.delete(current, version_id)
            st.session_state[version_key] = graph_versions.AUTO_VERSION
            st.session_state["_library_notice"] = ("info", "已删除当前自定义图谱版本")
            st.rerun()

    with toolbar[3].popover("批量操作", width="stretch"):
        for action, label in (
            ("confirm_official", "确认全部官方引用"),
            ("reject_low_confidence", "驳回低置信候选"),
            ("clear_notes", "清空全部人工备注"),
            (
                "toggle_rejected",
                "隐藏已驳回节点" if show_rejected else "显示已驳回节点",
            ),
        ):
            if st.button(
                label,
                key=f"cite_graph_batch_{action}_{current.id}",
                width="stretch",
                disabled=action == "toggle_rejected" and rejected_count == 0,
            ):
                st.session_state[f"cite_graph_batch_pending_{current.id}"] = action
                st.rerun()
    with toolbar[4].popover("导出", width="stretch"):
        for action, label in (
            ("outline", "方法演进大纲"),
            ("comparison", "复现方案对比"),
            ("png", "导出当前图谱 PNG"),
        ):
            if st.button(
                label,
                key=f"cite_graph_export_{action}_{current.id}",
                width="stretch",
            ):
                st.session_state[f"cite_graph_export_pending_{current.id}"] = action
                st.rerun()

    widget_key = f"cite_graph_{current.id}"
    success_key = f"cite_graph_success_notice_{current.id}"
    success_notice = _decode(st.session_state.get(success_key))
    _component()(
        key=widget_key,
        data=_payload(
            project,
            current,
            relations,
            show_rejected=bool(show_rejected),
            version_id=version_id,
            version_item=version_item,
            export_request=_decode(st.session_state.get(
                f"cite_graph_export_request_{current.id}"
            )),
            show_hidden=show_hidden,
            recommendations=recommendation_items(current),
            selected_relation_id=graph_preferences.selected_relation_id(current),
            layout=str(selected_layout),
            success_notice=success_notice,
        ),
        default={},
        height=520,
        on_graph_event_change=lambda: _apply_action(
            project, current, widget_key, version_id
        ),
    )
    if success_notice:
        st.session_state.pop(success_key, None)

    limit = int(st.session_state.get(f"limit_{current.id}", 10))
    if st.button(
        "拓展相关文献",
        key=f"cite_graph_expand_{current.id}",
        width="stretch",
        help=f"从 OpenAlex 获取相似文献与共引文献，最多 {limit} 条；仅作为待核验推荐",
    ):
        with st.spinner("正在查询 OpenAlex 的相似文献与共引文献…"):
            try:
                report = graph_recommendations.fetch_graph_recommendations(
                    current, limit=limit
                )
            except Exception as exc:  # noqa: BLE001 - network failures stay visible
                st.error(f"拓展推荐失败：{exc}")
            else:
                _save_recommendation_items(
                    current, [item.to_dict() for item in report.items]
                )
                if report.errors:
                    st.warning("；".join(report.errors))
                if not report.items:
                    st.info("没有可补充的推荐；已入库或已驳回的文献不会重复出现。")
                st.rerun()

    candidate_key = f"cite_graph_candidate_{current.id}"
    candidate_id = str(st.session_state.get(candidate_key) or "")
    if candidate_id:
        candidate = repo.get(Paper, candidate_id)
        if candidate is not None and candidate.project_id == project.id:
            _candidate_dialog(project, candidate, candidate_key)
        else:
            st.session_state.pop(candidate_key, None)

    note_key = f"cite_graph_note_{current.id}"
    relation_id = str(st.session_state.get(note_key) or "")
    if relation_id:
        relation = repo.get(PaperRelation, relation_id)
        if (
            relation is not None
            and relation.project_id == project.id
            and current.id in (relation.from_paper_id, relation.to_paper_id)
        ):
            _note_dialog(current, relation, note_key, version_id)
        else:
            st.session_state.pop(note_key, None)

    add_key = f"cite_graph_add_{current.id}"
    if st.session_state.get(add_key):
        _add_dialog(project, current, add_key)

    batch_key = f"cite_graph_batch_pending_{current.id}"
    batch_action = str(st.session_state.get(batch_key) or "")
    if batch_action:
        _batch_dialog(
            project,
            current,
            relations,
            batch_action,
            version_id,
            batch_key,
        )

    export_key = f"cite_graph_export_pending_{current.id}"
    export_action = str(st.session_state.get(export_key) or "")
    if export_action == "outline":
        _outline_dialog(project, current, relations, version_id, export_key)
    elif export_action == "comparison":
        _comparison_dialog(project, current, relations, export_key)
    elif export_action == "png":
        _png_dialog(current, export_key)
