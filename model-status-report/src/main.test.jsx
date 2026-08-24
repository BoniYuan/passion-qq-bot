// @vitest-environment jsdom
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { reportMock } = vi.hoisted(() => ({ reportMock: vi.fn() }));
vi.mock("./api", () => ({ api: { report: reportMock } }));
vi.mock("html-to-image", () => ({ toPng: vi.fn() }));

import { App } from "./main";

const SAMPLE = {
  range: "15m",
  range_label: "最近 15 分钟",
  from: "2026-08-20T19:30:00Z",
  to: "2026-08-20T21:00:00Z",
  totals: { requests: 12, success_rate: 95.5 },
  groups: [{
    id: 76,
    name: "很长的酒馆按量模型分组名称",
    totals: { requests: 12, success_rate: 95.5 },
    models: [{ model_name: "very-long-model-name/claude-opus", platform: "anthropic", total: 12, success_rate: 95.5, empty_count: 1, empty_rate: 8.33, failure_count: 0, average_duration: 5, average_speed: 42, badges: ["recommended"] }],
  }, {
    id: 77,
    name: "第二分组",
    totals: { requests: 0, success_rate: null },
    models: [{ model_name: "idle-model", platform: "openai", total: 0, success_rate: null, empty_count: 0, empty_rate: null, failure_count: 0, average_duration: null, average_speed: null, badges: [] }, { model_name: "gpt-4o", platform: "openai", total: 3, success_rate: 100, empty_count: 0, empty_rate: 0, failure_count: 0, average_duration: 2, average_speed: 20, badges: [] }],
  }],
};

describe("Passion report UI", () => {
  let container;
  let root;
  beforeEach(() => {
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    reportMock.mockReset().mockResolvedValue(SAMPLE);
    window.history.replaceState({}, "", "/");
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("renders summary, time range, and stable screenshot markers", async () => {
    await act(async () => { root.render(<App />); });
    await act(async () => { await Promise.resolve(); });
    const report = container.querySelector("[data-report-root]");
    expect(report.querySelector(".toolbar")).toBeNull();
    expect(container.querySelector(".toolbar").parentElement).not.toBe(report);
    expect(report.dataset.reportReady).toBe("true");
    expect(container.querySelector("[data-report-time-range]").textContent).toContain("至");
    expect(container.textContent).toContain("12");
    expect(container.textContent).toContain("95.50%");
    expect(container.textContent).toContain("very-long-model-name/claude-opus");
    expect(container.querySelector(".model-grid")).not.toBeNull();
    expect(container.querySelectorAll(".model-card")).toHaveLength(2);
    expect(container.querySelector(".model-grid").className).toContain("model-grid");
  });

  it("filters by group and retains loaded content after a refresh error", async () => {
    await act(async () => { root.render(<App />); });
    await act(async () => { await Promise.resolve(); });
    const select = container.querySelector("select");
    await act(async () => { select.value = "76"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(reportMock).toHaveBeenCalledWith("76");
    reportMock.mockRejectedValueOnce(new Error("刷新失败"));
    await act(async () => { container.querySelector(".button").click(); await Promise.resolve(); });
    expect(container.textContent).toContain("very-long-model-name/claude-opus");
    expect(container.textContent).toContain("刷新失败");
  });

  it("renders the empty state without blocking screenshot readiness", async () => {
    reportMock.mockResolvedValueOnce({ ...SAMPLE, totals: { requests: 0, success_rate: null }, groups: [] });
    await act(async () => { root.render(<App />); });
    await act(async () => { await Promise.resolve(); });
    expect(container.textContent).toContain("暂无调用数据");
    expect(container.querySelector("[data-report-root]").dataset.reportReady).toBe("true");
    expect(container.querySelector("[data-report-time-range]").textContent).not.toContain("--");
  });

  it("searches all groups by model or platform and includes inactive matches", async () => {
    await act(async () => { root.render(<App />); });
    await act(async () => { await Promise.resolve(); });
    const input = container.querySelector('input[aria-label="搜索分组名称、模型名称或平台"]');
    await act(async () => { const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; setter.call(input, "OPENAI"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(container.textContent).toContain("gpt-4o");
    expect(container.textContent).not.toContain("idle-model");
    expect(container.textContent).toContain("第二分组");
    expect(container.textContent).not.toContain("very-long-model-name/claude-opus");
    await act(async () => { const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; setter.call(input, "does-not-exist"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(container.textContent).toContain("未找到匹配模型");
  });

  it("filters by group name and keeps all active models in the matched group", async () => {
    await act(async () => { root.render(<App />); });
    await act(async () => { await Promise.resolve(); });
    const input = container.querySelector('input[aria-label="搜索分组名称、模型名称或平台"]');
    await act(async () => { const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; setter.call(input, "第二分组"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(container.textContent).toContain("第二分组");
    expect(container.textContent).toContain("gpt-4o");
    expect(container.textContent).not.toContain("idle-model");
    expect(container.textContent).not.toContain("very-long-model-name/claude-opus");
  });
});
