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

  const total = items.reduce((sum, item) => sum + item.price_cents * item.qty, 0);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await api.checkout(
        { items: items.map((item) => ({ sku: item.sku, qty: item.qty })), address },
        crypto.randomUUID(),
      );
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
