import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, Check, ChevronDown, Download, FileJson, Gauge, LoaderCircle, RefreshCw, Search, X } from "lucide-react";
import { toPng } from "html-to-image";
import { api } from "./api";
import "./styles.css";
import "./search.css";
import "./cute-theme.css";

const fmt = (value, digits = 1) => value == null ? "--" : Number(value).toFixed(digits);
const fmtTime = (value) => value ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value)) : "--";

function Button({ children, icon: Icon, busy, ...props }) {
  return <button className="button" {...props}>{busy ? <LoaderCircle className="spin" /> : Icon && <Icon />}{children}</button>;
}

function Toast({ toast, close }) {
  if (!toast) return null;
  return <div className={`toast ${toast.type}`} role="status"><span>{toast.type === "error" ? <X /> : <Check />}</span><p>{toast.message}</p><button aria-label="关闭" onClick={close}><X /></button></div>;
}

const badgeLabels = { recommended: "推荐", long: "耗时长", slow: "缓慢" };

function ModelCardHeader({ model }) {
  return <div className="model-title"><div className="model-name-row"><span className="model-spark" aria-hidden="true" /> <h3 title={model.model_name}>{model.model_name}</h3></div>{model.platform && <span>{model.platform}</span>}</div>;
}

function ModelCardStatus({ model, hasData }) {
  return <div className="card-meta"><span className="status-caption">{hasData ? "当前状态" : "等待数据"}</span><div className="status-pills">{hasData ? model.badges.map((badge) => <span key={badge} className={`badge ${badge}`}>{badgeLabels[badge]}</span>) : <span className="badge no-data">暂无数据</span>}</div><span className="speed">速率 {model.average_speed == null ? "--" : `${fmt(model.average_speed, 2)} tk/s`}</span></div>;
}

function ModelResultBar({ model, hasData }) {
  const successWidth = hasData ? model.success_rate || 0 : 0;
  const emptyWidth = hasData ? model.empty_rate || 0 : 0;
  const failureWidth = hasData ? model.failure_count / model.total * 100 : 0;
  return <div className="result-bar" aria-label={hasData ? "监控结果分布" : "暂无监控数据"}>{successWidth > 0 && <span className="success" style={{ width: `${successWidth}%` }} />}{emptyWidth > 0 && <span className="empty-bar" style={{ width: `${emptyWidth}%` }} />}{failureWidth > 0 && <span className="failure" style={{ width: `${failureWidth}%` }} />}</div>;
}

function ModelCardMetrics({ model, hasData }) {
  const successful = hasData && model.success_rate != null ? Math.round(model.total * model.success_rate / 100) : 0;
  return <><dl className="metrics"><div><dt>平均首字</dt><dd>{model.average_first_token == null ? "--" : `${fmt(model.average_first_token)}s`}</dd></div><div><dt>平均用时</dt><dd>{model.average_duration == null ? "--" : `${fmt(model.average_duration)}s`}</dd></div><div><dt>成功率</dt><dd>{model.success_rate == null ? "--" : `${fmt(model.success_rate, 2)}%`}</dd></div><div><dt>空回率</dt><dd>{model.empty_rate == null ? "--" : `${fmt(model.empty_rate, 2)}%`}</dd></div></dl><div className="result-counts"><div><span>成功</span><strong>{hasData ? successful : "--"}</strong></div><div><span>失败</span><strong>{hasData ? model.failure_count : "--"}</strong></div><div><span>空回</span><strong>{hasData ? model.empty_count : "--"}</strong></div></div></>;
}

function ModelCard({ model }) {
  const hasData = model.total > 0;
  return <article className={`model-card ${hasData ? "" : "inactive"}`}>
    <ModelCardHeader model={model} />
    <ModelCardStatus model={model} hasData={hasData} />
    <ModelResultBar model={model} hasData={hasData} />
    <ModelCardMetrics model={model} hasData={hasData} />
  </article>;
}

function App() {
  const [report, setReport] = useState(null);
  const [allGroups, setAllGroups] = useState([]);
  const [selectedGroup, setSelectedGroup] = useState("");
  const query = new URLSearchParams(window.location.search);
  const requestedSearch = query.get("search") || "";
  const [search, setSearch] = useState(requestedSearch);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState(null);
  const reportRef = useRef(null);
  const requestedGroup = query.get("group_id") || "";
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const visibleGroups = (report?.groups || []).map((group) => {
    const groupMatches = Boolean(normalizedSearch && group.name.toLocaleLowerCase().includes(normalizedSearch));
    const models = group.models.filter((model) => {
      const matches = !normalizedSearch || groupMatches || `${model.model_name} ${model.platform}`.toLocaleLowerCase().includes(normalizedSearch);
      return matches && model.total > 0 && (!selectedGroup || normalizedSearch || String(group.id) === selectedGroup);
    });
    return { ...group, models };
  }).filter((group) => group.models.length > 0);
  const notify = useCallback((message, type = "success") => { setToast({ message, type }); window.setTimeout(() => setToast(null), 4000); }, []);
  const load = useCallback(async (groupId = selectedGroup, quiet = false) => {
    if (!quiet) setLoading(true);
    try { const next = await api.report(groupId); setReport(next); if (!groupId) setAllGroups(next.groups.map(({ id, name }) => ({ id, name }))); }
    catch (error) { notify(error.message, "error"); }
    finally { setLoading(false); }
  }, [notify, selectedGroup]);

  useEffect(() => { if (requestedGroup && selectedGroup !== requestedGroup) setSelectedGroup(requestedGroup); }, [requestedGroup, selectedGroup]);
  useEffect(() => { load(selectedGroup || requestedGroup); }, [load, selectedGroup, requestedGroup]);
  useEffect(() => { const timer = window.setInterval(() => load(selectedGroup, true), 30000); return () => window.clearInterval(timer); }, [load, selectedGroup]);

  const exportPng = async () => {
    const node = reportRef.current;
    const shell = document.querySelector(".app-shell");
    if (!node) return;
    const previous = { nodeWidth: node.style.width, shellOverflow: shell?.style.overflow, bodyOverflow: document.body.style.overflow };
    try {
      const width = Math.ceil(Math.max(node.scrollWidth, node.getBoundingClientRect().width));
      const exportWidth = width + 48;
      const height = Math.ceil(node.scrollHeight);
      node.style.width = `${exportWidth}px`;
      if (shell) shell.style.overflow = "visible";
      document.body.style.overflow = "visible";
      const dataUrl = await toPng(node, { cacheBust: true, pixelRatio: 2, backgroundColor: "#f4f8ff", width: exportWidth, height, style: { width: `${exportWidth}px`, minWidth: `${exportWidth}px`, maxWidth: "none", overflow: "visible", paddingRight: "48px" } });
      const link = document.createElement("a"); link.download = "Passion API 模型状态报告.png"; link.href = dataUrl; link.click();
    } catch { notify("PNG 导出失败", "error"); }
    finally { node.style.width = previous.nodeWidth; if (shell) shell.style.overflow = previous.shellOverflow; document.body.style.overflow = previous.bodyOverflow; }
  };

  const ready = Boolean(report && !loading && report.from && report.to);
  return <div className="app-shell">
    <nav className="topnav"><a className="brand" href="#top"><img src="/passion-logo.png" alt="Passion API" /><span><strong>Passion API</strong><small>状态报告</small></span></a><div className="nav-label"><span className="live-dot" /> 模型状态中心</div></nav>
    <section className="toolbar" aria-label="报告工具栏"><label>模型分组<div className="select-wrap"><select value={selectedGroup} onChange={(event) => setSelectedGroup(event.target.value)}><option value="">全部分组</option>{allGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select><ChevronDown /></div></label><label className="search-label">搜索分组/模型<div className="search-wrap"><Search /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="输入分组名、模型名或平台" aria-label="搜索分组名称、模型名称或平台" />{search && <button type="button" onClick={() => setSearch("")} aria-label="清除搜索"><X /></button>}</div></label><div className="toolbar-note"><Gauge /> 固定统计最近 15 分钟</div><div className="toolbar-actions"><Button icon={RefreshCw} busy={loading} disabled={loading} onClick={() => load(selectedGroup)}>刷新</Button><button className="icon-button" aria-label="导出 PNG" title="导出 PNG" disabled={!report} onClick={exportPng}><Download /></button><button className="icon-button" aria-label="导出 JSON" title="导出 JSON" disabled={!report} onClick={() => { window.location.href = `/api/reports/export${selectedGroup ? `?group_id=${selectedGroup}` : ""}`; }}><FileJson /></button></div></section>
    <main className="report" id="top" ref={reportRef} data-report-root data-report-ready={ready ? "true" : "false"}>
      <header className="report-header"><div className="report-heading"><p className="eyebrow"><span /> PASSION API · MODEL HEALTH</p><h1>模型状态报告</h1><p>最近 15 分钟模型调用质量与渠道健康概览</p><div className="report-guide" role="note"><strong>统计窗口</strong><span data-report-time-range>最近 15 分钟：{fmtTime(report?.from)} 至 {fmtTime(report?.to)}</span><span className="guide-key"><i className="key-success" />成功 <i className="key-empty" />空回 <i className="key-failure" />失败</span></div></div></header>
      {loading && !report ? <div className="empty"><LoaderCircle className="spin" /><p>正在读取模型监控数据</p></div> : visibleGroups.length ? visibleGroups.map((group) => <section className="model-group" key={group.id}><div className="section-title"><div className="section-heading"><h2>{group.name}</h2><span>{group.models.length} 个模型</span></div><div className="section-stats"><span>请求 <b>{group.totals.requests}</b></span><span>成功率 <b>{group.totals.success_rate == null ? "--" : `${fmt(group.totals.success_rate, 2)}%`}</b></span></div></div><div className="model-grid">{group.models.map((model) => <ModelCard key={`${model.platform}-${model.model_name}`} model={model} />)}</div></section>) : <div className="empty"><Activity /><h3>{report ? (normalizedSearch ? "未找到匹配模型" : "暂无调用数据") : "报告加载失败"}</h3><p>{report ? (normalizedSearch ? "请尝试其他模型名称或平台关键词。" : "所选分组最近 15 分钟没有模型调用记录。") : "请检查 Passion 服务连接后刷新。"}</p></div>}
    </main><Toast toast={toast} close={() => setToast(null)} />
  </div>;
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<React.StrictMode><App /></React.StrictMode>);
export { App, ModelCard, fmtTime };
