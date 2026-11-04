import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { cartItems, getSession, login, logout, type Role, type Session } from "./lib/api";
import Home from "./pages/Home";
import Product from "./pages/Product";
import Cart from "./pages/Cart";
import Orders from "./pages/Orders";
import Ops from "./pages/Ops";
import Agent from "./pages/Agent";

function Header({ session, onSession }: { session: Session | null; onSession: (s: Session | null) => void }) {
  const [cartCount, setCartCount] = useState(0);
  const [showLogin, setShowLogin] = useState(false);
  const [userId, setUserId] = useState("demo-customer");
  const [role, setRole] = useState<Role>("customer");
  const navigate = useNavigate();

  useEffect(() => {
    const update = () => setCartCount(cartItems().reduce((sum, item) => sum + item.qty, 0));
    update();
    window.addEventListener("cart-updated", update);
    return () => window.removeEventListener("cart-updated", update);
  }, []);

  const submit = async () => {
    const next = await login(userId, role);
    onSession(next);
    setShowLogin(false);
    if (role !== "customer") navigate("/ops");
  };

  return (
    <header className="topbar">
      <div className="brand" onClick={() => navigate("/")}>
        <span className="brand-mark">云</span>
        <div>
          <strong>云集商城</strong>
          <small>Harness 电商参考实现</small>
        </div>
      </div>
      <nav className="nav">
        <NavLink to="/" end>商城</NavLink>
        <NavLink to="/orders">订单</NavLink>
        <NavLink to="/agent">智能助手</NavLink>
        {(session?.role === "operator" || session?.role === "admin") && <NavLink to="/ops">运营后台</NavLink>}
      </nav>
      <div className="actions">
        <button className="ghost" onClick={() => navigate("/cart")}>
          购物车 {cartCount > 0 && <span className="badge">{cartCount}</span>}
        </button>
        {session ? (
          <button className="ghost" onClick={() => { logout(); onSession(null); }}>
            {session.user_id} · {session.role === "customer" ? "顾客" : "运营"} / 退出
          </button>
        ) : (
          <button className="primary small" onClick={() => setShowLogin((value) => !value)}>身份登录</button>
        )}
      </div>
      {showLogin && (
        <div className="login-panel">
          <label>
            用户标识
            <input value={userId} onChange={(event) => setUserId(event.target.value)} />
          </label>
          <label>
            角色
            <select value={role} onChange={(event) => setRole(event.target.value as Role)}>
              <option value="customer">顾客</option>
              <option value="operator">运营</option>
              <option value="admin">管理员</option>
            </select>
          </label>
          <button className="primary" onClick={submit}>进入</button>
          <p className="hint">演示环境直接签发身份令牌，生产环境应接统一登录。</p>
        </div>
      )}
    </header>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(getSession());
  return (
    <BrowserRouter>
      <Header session={session} onSession={setSession} />
      <main className="page">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/product/:id" element={<Product />} />
          <Route path="/cart" element={<Cart />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/ops" element={<Ops />} />
          <Route path="/agent" element={<Agent />} />
        </Routes>
      </main>
      <footer className="footer">
        <span>微服务 · Harness 控制面 · 沙箱 · MCP · 三层记忆</span>
        <span>演示数据仅用于本地验证</span>
      </footer>
    </BrowserRouter>
  );
}
