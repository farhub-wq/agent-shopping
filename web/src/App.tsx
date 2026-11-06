import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { ApiError, cartItems, getSession, login, logout, refreshSession, register, type Session } from "./lib/api";
import Home from "./pages/Home";
import Product from "./pages/Product";
import Cart from "./pages/Cart";
import Orders from "./pages/Orders";
import Ops from "./pages/Ops";
import Audit from "./pages/Audit";
import Mfa from "./pages/Mfa";
import Chargebacks from "./pages/Chargebacks";
import Agent from "./pages/Agent";

const PHONE_PATTERN = /^1[3-9]\d{9}$/;

// 与服务端 identity 的注册/登录校验保持一致，先在前端拦住明显不合规的输入，
// 避免用户只收到笼统的“请求参数不合法”。
function validateCredentials(phone: string, password: string): string {
  if (!PHONE_PATTERN.test(phone)) return "手机号需为 11 位大陆手机号，例如 13800000000";
  if (password.length < 8 || password.length > 72) return "密码长度需为 8-72 位";
  return "";
}

function Header({ session, onSession }: { session: Session | null; onSession: (s: Session | null) => void }) {
  const [cartCount, setCartCount] = useState(0);
  const [showLogin, setShowLogin] = useState(false);
  const [mode, setMode] = useState<"login" | "register">("login");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  // MFA 两步登录：密码通过但账号启用了动态码时，进入待验证状态，保留已输入的手机号与密码
  const [mfaPending, setMfaPending] = useState(false);
  const [otp, setOtp] = useState("");
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
    // 先做本地校验，规则不合规时直接给出具体原因，不发无意义的请求
    const invalid = validateCredentials(phone.trim(), password);
    if (invalid) {
      setError(invalid);
      return;
    }
    try {
      // 注册成功后立刻登录建立会话，用户不用再输一次密码
      if (mode === "register") await register(phone.trim(), password);
      const next = await login(phone.trim(), password);
      onSession(next);
      setShowLogin(false);
      setMfaPending(false);
      setOtp("");
      // 运营/管理员登录后直接进入后台，顾客仍留在商城流程；角色来自服务端返回值
      if (next.role !== "customer") navigate("/ops");
    } catch (err) {
      // 账号启用了双因素认证：密码已通过，切到动态码输入而不是当作失败
      if (err instanceof ApiError && err.code === "MFA_REQUIRED") {
        setMfaPending(true);
        return;
      }
      setError(err instanceof Error ? err.message : "操作失败");
    }
  };

  const submitOtp = async () => {
    setError("");
    if (!/^\d{6}$/.test(otp)) {
      setError("请输入 6 位动态验证码");
      return;
    }
    try {
      const next = await login(phone.trim(), password, otp);
      onSession(next);
      setShowLogin(false);
      setMfaPending(false);
      setOtp("");
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
        {/* 审计日志是更敏感的只读数据，仅管理员可见。 */}
        {session?.role === "admin" && <NavLink to="/audit">审计日志</NavLink>}
        {/* 管理员自己的双因素认证设置，仅管理员可见。 */}
        {session?.role === "admin" && <NavLink to="/mfa">安全设置</NavLink>}
        {/* 拒付处置是资金风险操作，运营与管理员都可见，服务端再校验。 */}
        {(session?.role === "operator" || session?.role === "admin") && <NavLink to="/chargebacks">拒付处理</NavLink>}
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
          {mfaPending ? (
            <>
              <p className="hint">该账号已启用双因素认证，请输入验证器中的 6 位动态验证码</p>
              <label>
                动态验证码
                <input
                  value={otp}
                  onChange={(event) => setOtp(event.target.value)}
                  placeholder="6 位数字"
                  inputMode="numeric"
                  maxLength={6}
                />
              </label>
              <button className="primary" onClick={submitOtp}>验证并登录</button>
              <button className="ghost small" onClick={() => { setMfaPending(false); setError(""); }}>返回</button>
            </>
          ) : (
            <>
              <label>
                手机号
                <input value={phone} onChange={(event) => setPhone(event.target.value)} placeholder="13800000000" />
              </label>
              <label>
                密码
                <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
              </label>
              <button className="primary" onClick={submit}>{mode === "login" ? "登录" : "注册并登录"}</button>
              {/* 把账号规则前置展示，避免用户试错后才发现格式要求 */}
              <p className="hint">手机号 11 位；密码 8-72 位</p>
              <button className="ghost small" onClick={() => setMode(mode === "login" ? "register" : "login")}>
                {mode === "login" ? "没有账号？去注册" : "已有账号？去登录"}
              </button>
            </>
          )}
          {error && <p className="form-error">{error}</p>}
        </div>
      )}
    </header>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(getSession());

  useEffect(() => {
    // access token 只在内存里，刷新页面后已丢失；挂载时靠 refresh Cookie 恢复一次会话。
    // 失败也同步 state（置空），否则 header 会残留旧身份而页面已按 localStorage 判定未登录。
    void refreshSession().then(setSession);
  }, []);
  return (
    <BrowserRouter>
      <Header session={session} onSession={setSession} />
      <main className="page">
        {/* 页面组件只负责渲染与交互，所有接口调用都封在 lib/api.ts 里。 */}
        <Routes>
          <Route path="/" element={<Home session={session} />} />
          <Route path="/product/:id" element={<Product />} />
          <Route path="/cart" element={<Cart />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/ops" element={<Ops />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/mfa" element={<Mfa />} />
          <Route path="/chargebacks" element={<Chargebacks />} />
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
