"""Browser-side window interactions for the global assistant panel.

Persistence remains in Streamlit session state and browser preferences. This
component only handles transient pointer geometry while the user drags.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

_JS = r"""
export default function(component) {
  const {data, setTriggerValue, parentElement} = component;
  const config = data && typeof data === "object" ? data : {};
  const doc = parentElement?.ownerDocument || document;
  const panel = doc.querySelector(".st-key-rp_assistant_panel");
  if (!panel) return;
  const root = doc.documentElement;
  const mode = config.mode === "docked" ? "docked" : "floating";
  const maximized = Boolean(config.maximized);
  const cleanup = [];
  let request = 0;

  doc.querySelectorAll("[data-rp-assistant-window-handle]").forEach(
    (handle) => handle.remove()
  );
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const emit = (type, payload) => setTriggerValue("assistant_window_event", {
    request_id: `${Date.now()}-${++request}`,
    type,
    ...payload,
  });

  function styleHandle(handle, css) {
    handle.dataset.rpAssistantWindowHandle = "1";
    handle.style.cssText = [
      "position:fixed", "z-index:1000002", "background:transparent",
      "touch-action:none", css,
    ].join(";");
    doc.body.appendChild(handle);
    cleanup.push(() => handle.remove());
  }

  function positionDockHandle(handle) {
    const width = Number(config.dock_width) || 420;
    const left = Math.max(0, window.innerWidth - panel.getBoundingClientRect().width);
    handle.style.left = `${left - 4}px`;
    handle.style.top = "0";
    handle.style.width = "9px";
    handle.style.height = "100vh";
    handle.style.cursor = "ew-resize";
  }

  if (mode === "docked") {
    root.style.setProperty("--rp-assistant-dock-width", `${config.dock_width}px`);
    const handle = doc.createElement("div");
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-label", "拖动调整 AI 助手宽度");
    styleHandle(handle, "width:9px;height:100vh;cursor:ew-resize");
    positionDockHandle(handle);
    const onResize = () => positionDockHandle(handle);
    window.addEventListener("resize", onResize);
    cleanup.push(() => window.removeEventListener("resize", onResize));

    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const onMove = (moveEvent) => {
        const width = clamp(window.innerWidth - moveEvent.clientX, 320, 600);
        root.style.setProperty("--rp-assistant-dock-width", `${width}px`);
        handle.style.left = `${moveEvent.clientX - 4}px`;
      };
      const onUp = (upEvent) => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        const width = clamp(window.innerWidth - upEvent.clientX, 320, 600);
        emit("resize_dock", {dock_width: Math.round(width)});
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });
  } else if (!maximized) {
    let dragHandle = null;
    const cornerHandles = [];
    const setGeometry = (geometry) => {
      if (geometry.left >= 0) root.style.setProperty("--rp-assistant-x", `${geometry.left}px`);
      else root.style.removeProperty("--rp-assistant-x");
      if (geometry.top >= 0) root.style.setProperty("--rp-assistant-y", `${geometry.top}px`);
      else root.style.removeProperty("--rp-assistant-y");
      root.style.setProperty("--rp-assistant-width", `${geometry.width}px`);
      root.style.setProperty("--rp-assistant-height", `${geometry.height}px`);
      if (dragHandle) {
        dragHandle.style.left = `${geometry.left + 10}px`;
        dragHandle.style.top = `${geometry.top + 6}px`;
      }
      for (const handle of cornerHandles) {
        const east = handle.dataset.corner.includes("e");
        const south = handle.dataset.corner.includes("s");
        handle.style.left = `${
          (east ? geometry.left + geometry.width : geometry.left) - 8
        }px`;
        handle.style.top = `${
          (south ? geometry.top + geometry.height : geometry.top) - 8
        }px`;
      }
    };
    const initialRect = panel.getBoundingClientRect();
    let geometry = {
      left: Number(config.float_x) >= 0 ? Number(config.float_x) : initialRect.left,
      top: Number(config.float_y) >= 0 ? Number(config.float_y) : initialRect.top,
      width: Number(config.float_width) || 420,
      height: Number(config.float_height) || 600,
    };
    setGeometry(geometry);

    const corners = [
      ["nw", "cursor:nwse-resize", (x, y, base) => ({
        left: base.left + (x - start.x), top: base.top + (y - start.y),
        width: base.width - (x - start.x), height: base.height - (y - start.y),
      })],
      ["ne", "cursor:nesw-resize", (x, y, base) => ({
        left: base.left, top: base.top + (y - start.y),
        width: base.width + (x - start.x), height: base.height - (y - start.y),
      })],
      ["sw", "cursor:nesw-resize", (x, y, base) => ({
        left: base.left + (x - start.x), top: base.top,
        width: base.width - (x - start.x), height: base.height + (y - start.y),
      })],
      ["se", "cursor:nwse-resize", (x, y, base) => ({
        left: base.left, top: base.top,
        width: base.width + (x - start.x), height: base.height + (y - start.y),
      })],
    ];
    const cornerLabels = {
      nw: "左上角", ne: "右上角", sw: "左下角", se: "右下角",
    };
    const rect = panel.getBoundingClientRect();
    let start = {x: 0, y: 0};
    for (const [name, cursor, calculate] of corners) {
      const handle = doc.createElement("div");
      handle.setAttribute(
        "aria-label",
        `拖动${cornerLabels[name] || "边角"}调整 AI 助手窗口`,
      );
      const x = name.includes("w") ? rect.left : rect.right;
      const y = name.includes("n") ? rect.top : rect.bottom;
      styleHandle(handle, `width:16px;height:16px;${cursor}`);
      handle.style.left = `${x - 8}px`;
      handle.style.top = `${y - 8}px`;
      handle.dataset.corner = name;
      cornerHandles.push(handle);
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        start = {x: event.clientX, y: event.clientY};
        const base = {...geometry};
        const onMove = (moveEvent) => {
          const proposed = calculate(
            moveEvent.clientX, moveEvent.clientY, base,
          );
          const width = clamp(proposed.width, 320, window.innerWidth - 16);
          const height = clamp(proposed.height, 360, window.innerHeight - 16);
          let left = proposed.left;
          let top = proposed.top;
          if (name.includes("w")) left = base.left + base.width - width;
          if (name.includes("n")) top = base.top + base.height - height;
          left = clamp(left, 8, window.innerWidth - width - 8);
          top = clamp(top, 8, window.innerHeight - height - 8);
          geometry = {left, top, width, height};
          setGeometry(geometry);
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          emit("resize_float", {
            float_x: Math.round(geometry.left),
            float_y: Math.round(geometry.top),
            float_width: Math.round(geometry.width),
            float_height: Math.round(geometry.height),
          });
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      });
    }
    setGeometry(geometry);

    dragHandle = doc.createElement("div");
    dragHandle.setAttribute("role", "button");
    dragHandle.setAttribute("aria-label", "拖动移动 AI 助手窗口");
    styleHandle(
      dragHandle,
      "width:42px;height:16px;cursor:move;border-radius:8px;" +
      "background:rgba(23,26,28,.06)",
    );
    const startDrag = (event) => {
      event.preventDefault();
      const origin = {
        x: event.clientX,
        y: event.clientY,
        left: geometry.left,
        top: geometry.top,
      };
      const onMove = (moveEvent) => {
        geometry.left = clamp(
          origin.left + moveEvent.clientX - origin.x,
          8,
          window.innerWidth - geometry.width - 8,
        );
        geometry.top = clamp(
          origin.top + moveEvent.clientY - origin.y,
          8,
          window.innerHeight - geometry.height - 8,
        );
        setGeometry(geometry);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        emit("move_float", {
          float_x: Math.round(geometry.left),
          float_y: Math.round(geometry.top),
        });
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    };
    dragHandle.addEventListener("pointerdown", startDrag);
    setGeometry(geometry);
  }

  return () => cleanup.forEach((callback) => callback());
}
"""


def component():
    return st.components.v2.component(
        "rp_assistant_window",
        js=_JS,
    )


def render(data: dict[str, Any], on_event) -> None:
    """Mount drag handles for the currently visible assistant panel."""
    try:
        component()(
            key="rp_assistant_window",
            data=data,
            default={},
            height=0,
            on_assistant_window_event_change=on_event,
        )
    except Exception:  # noqa: BLE001 - resizing is optional, controls still work
        return
