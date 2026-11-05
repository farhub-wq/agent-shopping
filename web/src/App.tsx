import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { cartItems, getSession, login, logout, refreshSession, register, type Session } from "./lib/api";
import Home from "./pages/Home";
import Product from "./pages/Product";
import Cart from "./pages/Cart";
import Orders from "./pages/Orders";
import Ops from "./pages/Ops";
import Agent from "./pages/Agent";

function Header({ session, onSession }: { session: Session | null; onSession: (s: Session | null) => void }) {
  const [cartCount, setCartCount] = useState(0);
  const [showLogin, setShowLogin] = useState(false);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    // 监听购物车广播事件，角标数量由此处统一派生，不在各页面各自维护。
    const update = () => setCartCount(cartItems().reduce((sum, item) => sum + item.qty, 0));
    update();
    window.addEventListener("cart-updated", update);
    return () => window.removeEventListener("cart-updated", update);
  }, []);

  const submit = async () => {
    setError("");
    try {
      // 注册成功后立刻登录建立会话，用户不用再输一次密码
      if (mode === "register") await register(phone, password);
      const next = await login(phone, password);
      onSession(next);
      setShowLogin(false);
      // 运营/管理员登录后直接进入后台，顾客仍留在商城流程；角色来自服务端返回值
      if (next.role !== "customer") navigate("/ops");
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
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
        {/* 运营后台入口只对运营/管理员开放；顾客侧不显示，服务端仍会再校验。 */}
        {(session?.role === "operator" || session?.role === "admin") && <NavLink to="/ops">运营后台</NavLink>}
      </nav>
      <div className="actions">
        <button className="ghost" onClick={() => navigate("/cart")}>
          购物车 {cartCount > 0 && <span className="badge">{cartCount}</span>}
        </button>
        {session ? (
          <button className="ghost" onClick={() => { void logout().finally(() => onSession(null)); }}>
            {session.user_id} · {session.role === "customer" ? "顾客" : "运营"} / 退出
          </button>
        ) : (
          <button className="primary small" onClick={() => setShowLogin((value) => !value)}>登录 / 注册</button>
        )}
      </div>
      {showLogin && (
        <div className="login-panel">
          <label>
            手机号
            <input value={phone} onChange={(event) => setPhone(event.target.value)} placeholder="13800000000" />
          </label>
          <label>
            密码
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
          </label>
          <button className="primary" onClick={submit}>{mode === "login" ? "登录" : "注册并登录"}</button>
          <button className="ghost small" onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "没有账号？去注册" : "已有账号？去登录"}
          </button>
          {error && <p className="hint">{error}</p>}
        </div>
      )}
    </header>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(getSession());

  useEffect(() => {
    // access token 只在内存里，刷新页面后已丢失；挂载时靠 refresh Cookie 恢复一次会话
    // 恢复失败保持未登录状态，不跳转、不弹错误
    void refreshSession().then((restored) => {
      if (restored) setSession(restored);
    });
  }, []);
  return (
    <BrowserRouter>
      <Header session={session} onSession={setSession} />
      <main className="page">
        {/* 页面组件只负责渲染与交互，所有接口调用都封在 lib/api.ts 里。 */}
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
