import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, getSession, money, type Chargeback } from "../lib/api";

const PAGE_SIZE = 20;

type StatusFilter = "" | "opened" | "won" | "lost";

const STATUS_LABEL: Record<string, string> = {
  opened: "待处置",
  won: "申诉成功",
  lost: "败诉",
};

// 拒付处置页：运营/管理员查看渠道拒付并裁决；真正权限由 payment 服务再校验。
export default function Chargebacks() {
  const session = getSession();
  const [status, setStatus] = useState<StatusFilter>("");
  const [rows, setRows] = useState<Chargeback[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState("");

  const load = async (targetPage: number, nextStatus = status) => {
    try {
      setError("");
      const data = await api.chargebacks({
        ...(nextStatus ? { status: nextStatus } : {}),
        page: targetPage,
        size: PAGE_SIZE,
      });
      setRows(data.items);
      setTotal(data.total);
      setPage(data.page);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  // 进入页面按当前登录身份取数；重新登录后 user_id 变化会再次触发
  useEffect(() => {
    if (session && session.role !== "customer") load(1);
  }, [session?.user_id]);

  if (!session || session.role === "customer") {
    return (
      <div className="empty">
        <h2>需要运营身份</h2>
        <p className="muted">拒付处理仅运营/管理员可见。</p>
        <Link className="primary" to="/agent">或先体验智能助手</Link>
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // 裁决是不可逆的资金动作，先弹窗确认，避免误点直接改掉争议单状态
  const resolve = async (row: Chargeback, outcome: "won" | "lost") => {
    const label = outcome === "won" ? "申诉成功（渠道退回资金）" : "败诉（确认损失）";
    if (!window.confirm(`确定将争议单 ${row.provider_case_id} 处置为「${label}」？`)) return;
    try {
      setError("");
      await api.resolveChargeback(row.id, outcome);
      await load(page);
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <div>
      <div className="tabs">
        <span className="chip active">拒付处理</span>
        <Link className="chip" to="/ops">运营后台</Link>
      </div>
      {error && <p className="error">{error}</p>}

      <div className="panel">
        <h2>渠道拒付</h2>
        {/* 状态筛选：全部 / 待处置 / 申诉成功 / 败诉，切换后回到第一页 */}
        <div className="filters">
          {([
            ["", "全部"],
            ["opened", "待处置"],
            ["won", "申诉成功"],
            ["lost", "败诉"],
          ] as [StatusFilter, string][]).map(([key, label]) => (
            <button
              key={key}
              className={status === key ? "chip active" : "chip"}
              onClick={() => {
                setStatus(key);
                setPage(1);
                load(1, key);
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <table className="table">
          <thead>
            <tr>
              <th>争议单号</th>
              <th>订单</th>
              <th>金额</th>
              <th>状态</th>
              <th>原因</th>
              <th>发起时间</th>
              <th>处置</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.provider_case_id}</td>
                <td className="muted">{row.order_id ? row.order_id.slice(0, 8) : "-"}</td>
                <td>{money(row.amount_cents)}</td>
                <td>
                  <span className={`status ${row.status}`}>{STATUS_LABEL[row.status] || row.status}</span>
                </td>
                <td className="muted">{row.reason || "-"}</td>
                <td>{new Date(row.opened_at).toLocaleString()}</td>
                <td>
                  {row.status === "opened" ? (
                    <div className="row-actions">
                      <button className="primary small" onClick={() => resolve(row, "won")}>申诉成功</button>
                      <button className="ghost small" onClick={() => resolve(row, "lost")}>败诉</button>
                    </div>
                  ) : (
                    <span className="muted">
                      {row.closed_at ? new Date(row.closed_at).toLocaleDateString() : "已处置"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <p className="muted center">暂无拒付记录。</p>}

        <div className="row-actions">
          <button className="ghost small" disabled={page <= 1} onClick={() => load(page - 1)}>上一页</button>
          <span className="hint">第 {page} / {totalPages} 页 · 共 {total} 条</span>
          <button className="ghost small" disabled={page >= totalPages} onClick={() => load(page + 1)}>下一页</button>
        </div>
      </div>
    </div>
  );
}
