import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, money } from "../lib/api";

const CATEGORIES = [
  { key: "", label: "全部" },
  { key: "digital", label: "数码" },
  { key: "apparel", label: "服饰" },
  { key: "beauty", label: "美妆" },
  { key: "home", label: "家居" },
];

export default function Home() {
  const [items, setItems] = useState<any[]>([]);
  const [category, setCategory] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");

  const load = async () => {
    try {
      const data = await api.feed({ q: query || undefined, category: category || undefined });
      setItems(data.items || []);
      setError("");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  useEffect(() => {
    load();
  }, [category]);

  return (
    <div>
      <section className="hero">
        <div>
          <p className="eyebrow">订单 · 库存 · 履约 · 售后</p>
          <h1>把电商的高风险动作，收进可验证的运行时</h1>
          <p className="lede">
            Agent 负责接待与建议，沙箱与证据链负责兜底。每一次工具调用都有权限上限、审批边界与 trace 账本。
          </p>
          <div className="hero-actions">
            <button className="primary" onClick={() => (window.location.href = "/agent")}>和智能助手聊聊</button>
            <button className="ghost" onClick={() => (window.location.href = "/ops")}>看运营后台</button>
          </div>
        </div>
        <div className="hero-card">
          <span>库存一致性</span>
          <strong>Redis + Lua 原子预占</strong>
          <span>订单状态</span>
          <strong>显式状态机 + Saga 补偿</strong>
          <span>Agent 安全</span>
          <strong>信任等级 → 权限上限</strong>
        </div>
      </section>

      <div className="filter-bar">
        <div className="chips">
          {CATEGORIES.map((item) => (
            <button
              key={item.key}
              className={category === item.key ? "chip active" : "chip"}
              onClick={() => setCategory(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="search">
          <input
            placeholder="搜索商品，如「耳机」「防晒」"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && load()}
          />
          <button className="primary small" onClick={load}>搜索</button>
        </div>
      </div>

      {error && <p className="error">{error}</p>}
      <div className="grid">
        {items.map((item) => (
          <Link className="card product" key={item.id} to={`/product/${item.id}`}>
            <img src={item.image_url} alt={item.title} loading="lazy" />
            <div className="card-body">
              <span className="tag">{item.category}</span>
              <h3>{item.title}</h3>
              <p className="muted">{item.subtitle}</p>
              <div className="price-row">
                <span className="price">{money(item.price_cents)}</span>
                <span className={item.available > 0 ? "stock" : "stock out"}>
                  {item.available > 0 ? `可售 ${item.available}` : "缺货"}
                </span>
              </div>
            </div>
          </Link>
        ))}
      </div>
      {!items.length && !error && <p className="muted center">暂无商品，请先启动后端服务。</p>}
    </div>
  );
}
