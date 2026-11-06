import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, getSession, type AuditLog } from "../lib/api";

const PAGE_SIZE = 20;

// 只读查询页：真正权限由 audit 服务再校验，前端仅做入口收敛。
export default function Audit() {
  const session = getSession();
  const [filters, setFilters] = useState({ action: "", target_type: "", target_id: "", actor_id: "" });
  const [rows, setRows] = useState<AuditLog[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");

  const load = async (targetPage: number, nextFilters = filters) => {
    try {
      setError("");
      const data = await api.listAuditLogs({ ...nextFilters, page: targetPage, size: PAGE_SIZE });
      setRows(data.items);
      setTotal(data.total);
      setPage(data.page);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  // 进入页面按当前登录身份取数；重新登录后 user_id 变化会再次触发
  useEffect(() => {
    if (session?.role === "admin") load(1);
  }, [session?.user_id]);

  if (session?.role !== "admin") {
    return (
      <div className="empty">
        <h2>需要管理员身份</h2>
        <p className="muted">审计日志仅管理员可见。</p>
        <Link className="primary" to="/agent">或先体验智能助手</Link>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const setField = (key: keyof typeof filters) => (value: string) =>
    setFilters((prev) => ({ ...prev, [key]: value }));

  return (
    <div>
      <div className="tabs">
        <span className="chip active">审计日志</span>
        <Link className="chip" to="/ops">运营后台</Link>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="panel">
        <h2>筛选</h2>
        {/* 筛选字段均为精确匹配；空值不参与查询，服务端按倒序分页返回 */}
        <div className="filters">
          <label>
            动作
            <input value={filters.action} onChange={(event) => setField("action")(event.target.value)} placeholder="如 order.ship" />
          </label>
          <label>
            对象类型
            <input value={filters.target_type} onChange={(event) => setField("target_type")(event.target.value)} placeholder="如 order" />
          </label>
          <label>
            对象 ID
            <input value={filters.target_id} onChange={(event) => setField("target_id")(event.target.value)} />
          </label>
          <label>
            操作人
            <input value={filters.actor_id} onChange={(event) => setField("actor_id")(event.target.value)} />
          </label>
          <button className="primary" onClick={() => load(1)}>查询</button>
        </div>
      </div>

      <div className="panel">
        <table className="table">
          <thead>
            <tr>
              <th>时间</th>
              <th>来源</th>
              <th>操作人</th>
              <th>动作</th>
              <th>对象</th>
              <th>变更</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{new Date(row.occurred_at).toLocaleString()}</td>
                <td>{row.service}</td>
                <td>{row.actor_id}{row.actor_role ? ` (${row.actor_role})` : ""}</td>
                <td>{row.action}</td>
                <td>{row.target_type}:{row.target_id}</td>
                <td className="muted">{snapshot(row)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <p className="muted center">暂无审计记录。</p>}
        {/* 分页只做上一页/下一页，够用且避免页码控件复杂度 */}
        <div className="row-actions">
          <button className="ghost small" disabled={page <= 1} onClick={() => load(page - 1)}>上一页</button>
          <span className="hint">第 {page} / {totalPages} 页 · 共 {total} 条</span>
          <button className="ghost small" disabled={page >= totalPages} onClick={() => load(page + 1)}>下一页</button>
        </div>
      </div>
    </div>
  );
}

// 变更前后快照只做摘要展示，避免整段 JSON 撑爆表格
function snapshot(row: AuditLog): string {
  const parts: string[] = [];
  if (row.before) parts.push(`前:${JSON.stringify(row.before)}`);
  if (row.after) parts.push(`后:${JSON.stringify(row.after)}`);
  return parts.join(" → ") || "-";
}
