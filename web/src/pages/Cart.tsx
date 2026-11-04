import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, cartItems, clearCart, money, saveCart, type CartItem } from "../lib/api";

export default function Cart() {
  const [items, setItems] = useState<CartItem[]>([]);
  const [address, setAddress] = useState({ receiver: "张三", phone: "13800001111", province: "上海", city: "上海", detail: "浦东新区世纪大道 100 号" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => setItems(cartItems()), []);

  // 本地购物车的增删改都立即回写 localStorage，刷新或跳页后状态不丢。
  const update = (sku: string, qty: number) => {
    const next = items.map((item) => (item.sku === sku ? { ...item, qty: Math.max(1, qty) } : item));
    setItems(next);
    saveCart(next);
  };

  const remove = (sku: string) => {
    const next = items.filter((item) => item.sku !== sku);
    setItems(next);
    saveCart(next);
  };

  // 合计由本地单价×数量派生，仅用于展示；最终金额以服务端下单结果为准。
  const total = items.reduce((sum, item) => sum + item.price_cents * item.qty, 0);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      // 下单必须携带幂等键；同时用 busy 禁用按钮，避免提交期间重复点击产生多单。
      await api.checkout(
        { items: items.map((item) => ({ sku: item.sku, qty: item.qty })), address },
        crypto.randomUUID(),
      );
      // 支付前先清空本地购物车；下单失败则保留，便于用户重试。
      clearCart();
      setItems([]);
      navigate("/orders");
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!items.length) {
    return (
      <div className="empty">
        <h2>购物车是空的</h2>
        <button className="primary" onClick={() => navigate("/")}>去逛逛</button>
      </div>
    );
  }

  return (
    <div className="two-col">
      <section className="panel">
        <h2>购物车</h2>
        {items.map((item) => (
          <div className="cart-row" key={item.sku}>
            <div>
              <strong>{item.title}</strong>
              <p className="muted">{item.sku} · {money(item.price_cents)}</p>
            </div>
            <input type="number" min={1} value={item.qty} onChange={(event) => update(item.sku, Number(event.target.value))} />
            <button className="ghost" onClick={() => remove(item.sku)}>移除</button>
          </div>
        ))}
        <div className="total-row">
          <span>合计</span>
          <strong className="price">{money(total)}</strong>
        </div>
      </section>

      <section className="panel">
        <h2>收货信息</h2>
        {/* 地址字段按固定顺序渲染，标签映射集中在这一处，新增字段需同步补充。 */}
        {Object.entries(address).map(([key, value]) => (
          <label key={key}>
            {({ receiver: "收货人", phone: "手机号", province: "省份", city: "城市", detail: "详细地址" } as Record<string, string>)[key]}
            <input value={value} onChange={(event) => setAddress({ ...address, [key]: event.target.value })} />
          </label>
        ))}
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={busy} onClick={submit}>
          {busy ? "提交中…" : `提交订单 ${money(total)}`}
        </button>
        <p className="hint">提交时携带幂等键，重复点击不会重复下单。</p>
      </section>
    </div>
  );
}
