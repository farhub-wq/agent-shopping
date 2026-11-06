import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { confirmMfa, disableMfa, enrollMfa, getMfaStatus, getSession } from "../lib/api";

// 管理员双因素认证设置：绑定（enroll→confirm）、关闭（需重新校验密码）。
// 二维码按确认结论以 otpauth 链接呈现，用户用 Authenticator 扫码或手动录入 secret。
export default function Mfa() {
  const session = getSession();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [secret, setSecret] = useState("");
  const [otpauthUrl, setOtpauthUrl] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = async () => {
    try {
      setError("");
      const status = await getMfaStatus();
      setEnabled(status.enabled);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "操作失败");
    }
  };

  // 进入页面按当前登录身份取状态；重新登录后 user_id 变化会再次触发
  useEffect(() => {
    if (session?.role === "admin") load();
  }, [session?.user_id]);

  if (session?.role !== "admin") {
    return (
      <div className="empty">
        <h2>需要管理员身份</h2>
        <p className="muted">安全设置仅管理员可用。</p>
        <Link className="primary" to="/agent">或先体验智能助手</Link>
      </div>
    );
  }

  const startEnroll = async () => {
    try {
      setError("");
      setNotice("");
      const data = await enrollMfa();
      setSecret(data.secret);
      setOtpauthUrl(data.otpauth_url);
      setCode("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "操作失败");
    }
  };

  const confirm = async () => {
    try {
      setError("");
      setNotice("");
      await confirmMfa(code);
      setSecret("");
      setOtpauthUrl("");
      setCode("");
      setNotice("双因素认证已启用");
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "操作失败");
    }
  };

  const disable = async () => {
    try {
      setError("");
      setNotice("");
      await disableMfa(password);
      setPassword("");
      setNotice("双因素认证已关闭");
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "操作失败");
    }
  };

  return (
    <div>
      <div className="tabs">
        <span className="chip active">安全设置</span>
        <Link className="chip" to="/ops">运营后台</Link>
        <Link className="chip" to="/audit">审计日志</Link>
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="hint">{notice}</p>}

      <div className="panel">
        <h2>双因素认证（TOTP）</h2>
        {enabled === null && <p className="muted">加载中…</p>}

        {enabled === true && (
          <>
            <p>当前账号已启用双因素认证，登录时需额外输入验证器中的 6 位动态验证码。</p>
            <div className="filters">
              <label>
                确认密码以关闭
                <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
              </label>
              <button className="ghost" onClick={disable} disabled={!password}>关闭双因素认证</button>
            </div>
          </>
        )}

        {enabled === false && !secret && (
          <>
            <p>当前账号未启用双因素认证。启用后，登录需在密码之外再验证一次动态验证码。</p>
            <button className="primary" onClick={startEnroll}>启用双因素认证</button>
          </>
        )}

        {enabled === false && secret && (
          <>
            <p>用 Authenticator（如 Google Authenticator / 1Password）扫描或手动录入以下密钥：</p>
            <p className="hint">密钥：{secret}</p>
            <p className="hint">
              链接：<a href={otpauthUrl}>{otpauthUrl}</a>
            </p>
            <div className="filters">
              <label>
                6 位动态验证码
                <input
                  value={code}
                  onChange={(event) => setCode(event.target.value)}
                  placeholder="6 位数字"
                  inputMode="numeric"
                  maxLength={6}
                />
              </label>
              <button className="primary" onClick={confirm} disabled={!/^\d{6}$/.test(code)}>确认绑定</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
