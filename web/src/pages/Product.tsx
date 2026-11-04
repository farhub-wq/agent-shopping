import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { addToCart, api, money } from "../lib/api";

export default function Product() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [data, setData] = useState<any>(null);
  const [qty, setQty] = useState(1);
  const [error, setError] = useState("");

  useEffect(() => {
    api.product(id).then(setData).catch((exc) => setError(exc.message));
  }, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">加载中…</p>;
  const { product, stock } = data;
  const soldOut = (stock.available ?? 0) <= 0;

  const cartPayload = () => ({
    sku: product.sku,
    product_id: product.id,
    title: product.title,
    price_cents: product.price_cents,
    qty,
  });

  return (
    <div className="detail">
      <img src={product.image_url} alt={product.title} />
      <div className="detail-body">
        <span className="tag">{product.category}</span>
        <h1>{product.title}</h1>
        <p className="muted">{product.subtitle}</p>
        <p className="detail-desc">{product.description}</p>
        <div className="attributes">
          {Object.entries(product.attributes || {}).map(([key, value]) => (
            <div key={key}>
              <span>{key}</span>
              <strong>{String(value)}</strong>
            </div>
          ))}
        </div>
        <div className="buy-row">
          <span className="price big">{money(product.price_cents)}</span>
          <span className={soldOut ? "stock out" : "stock"}>
            {soldOut ? "暂时缺货" : `可售 ${stock.available} 件`}
          </span>
        </div>
        <div className="qty-row">
          <span>数量</span>
          <input
            type="number"
            min={1}
            max={99}
            value={qty}
            onChange={(event) => setQty(Math.max(1, Math.min(99, Number(event.target.value))))}
          />
        </div>
        <div className="hero-actions">
          <button className="primary" disabled={soldOut} onClick={() => { addToCart(cartPayload()); navigate("/cart"); }}>
            立即购买
          </button>
          <button className="ghost" disabled={soldOut} onClick={() => { addToCart(cartPayload()); }}>
            加入购物车
          </button>
        </div>
        <p className="hint">下单会经过库存预占 → 支付 → 履约；任一环节失败都会补偿回滚。</p>
      </div>
    </div>
  );
}
