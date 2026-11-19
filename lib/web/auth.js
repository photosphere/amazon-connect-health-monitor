/* auth.js — Cognito 登录门禁 + API 调用封装
 *
 *  1) 页面上覆盖登录界面(登录 / 首次登录改密 / 绑定验证器 App / MFA 验证码 / 忘记密码),
 *     直接调用 Cognito User Pool REST 接口(USER_PASSWORD_AUTH, 无需 SDK)。
 *     MFA 使用 TOTP: 首次登录返回 MFA_SETUP → AssociateSoftwareToken → VerifySoftwareToken;
 *     之后每次登录返回 SOFTWARE_TOKEN_MFA, 输入验证器 App 上的 6 位验证码。
 *  2) IdToken / RefreshToken 存 sessionStorage(关闭标签页即清除), 过期前静默刷新,
 *     最长会话 24 小时。
 *  3) window.Api.request(method, path, params, body) 统一带 Authorization 头调用
 *     API Gateway; 401/403 时自动刷新 Token 重试一次。
 *  4) 认证成功后调用 window.MonitorApp.boot({email})。
 *
 * 依赖 aws-config.js(部署脚本生成): window.__AWS_CONFIG__ = {region, userPoolId, clientId, apiUrl}
 * 本地开发: __AWS_CONFIG__.mock = true 时跳过登录, 请求交给 window.__MOCK_API__ 处理。
 */
(function () {
  "use strict";

  var CFG = window.__AWS_CONFIG__ || {};
  var IDP = "https://cognito-idp." + CFG.region + ".amazonaws.com/";
  var SK_TOKEN = "cm_id_token", SK_EXP = "cm_expires_at", SK_REFRESH = "cm_refresh_token",
      SK_LOGIN = "cm_login_ts", SK_EMAIL = "cm_email";
  var MAX_SESSION = 24 * 3600;
  var idToken = "";
  var pendingEmail = "", pendingSession = "";
  var overlay, statusEl;

  function now() { return Math.floor(Date.now() / 1000); }

  function saveSession(r) {
    sessionStorage.setItem(SK_TOKEN, r.IdToken);
    sessionStorage.setItem(SK_EXP, String(now() + (r.ExpiresIn || 3600)));
    if (r.RefreshToken) sessionStorage.setItem(SK_REFRESH, r.RefreshToken);
    if (!sessionStorage.getItem(SK_LOGIN)) sessionStorage.setItem(SK_LOGIN, String(now()));
    idToken = r.IdToken;
  }
  function clearSession() {
    [SK_TOKEN, SK_EXP, SK_REFRESH, SK_LOGIN, SK_EMAIL].forEach(function (k) { sessionStorage.removeItem(k); });
    idToken = "";
  }
  function sessionTooOld() {
    var ts = parseInt(sessionStorage.getItem(SK_LOGIN) || "0", 10);
    return ts && now() - ts >= MAX_SESSION;
  }
  function tokenExpired() {
    var exp = parseInt(sessionStorage.getItem(SK_EXP) || "0", 10);
    return !exp || now() >= exp - 60;
  }
  function emailFromToken(tok) {
    try {
      var p = tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      var json = decodeURIComponent(atob(p).split("").map(function (c) {
        return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
      }).join(""));
      return JSON.parse(json).email || "";
    } catch (e) { return ""; }
  }

  function idp(target, body) {
    return fetch(IDP, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "AWSCognitoIdentityProviderService." + target },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) throw new Error(data.message || data.Message || data.__type || "请求失败");
        return data;
      });
    });
  }

  var refreshing = null;
  function refreshToken() {
    if (refreshing) return refreshing;
    var rt = sessionStorage.getItem(SK_REFRESH);
    if (!rt || sessionTooOld()) { clearSession(); return Promise.reject(new Error("会话已过期，请重新登录")); }
    refreshing = idp("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: CFG.clientId, AuthParameters: { REFRESH_TOKEN: rt },
    }).then(function (d) {
      if (!d.AuthenticationResult || !d.AuthenticationResult.IdToken) throw new Error("刷新 Token 失败");
      saveSession(d.AuthenticationResult);
      return idToken;
    }).catch(function (e) { clearSession(); throw e; })
      .finally(function () { refreshing = null; });
    return refreshing;
  }

  // ---- API 调用 ----
  function buildUrl(path, params) {
    var url = (CFG.apiUrl || "").replace(/\/$/, "") + path;
    var qs = Object.keys(params || {}).filter(function (k) { return params[k] != null && params[k] !== ""; })
      .map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
    return qs ? url + "?" + qs : url;
  }

  function request(method, path, params, body) {
    if (CFG.mock && window.__MOCK_API__) return window.__MOCK_API__(method, path, params || {}, body);
    function doFetch() {
      var opts = { method: method, headers: { Authorization: idToken, "Content-Type": "application/json" } };
      if (body !== undefined) opts.body = JSON.stringify(body);
      return fetch(buildUrl(path, params), opts).then(function (res) {
        return res.text().then(function (txt) {
          var data = {};
          try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = { error: txt }; }
          if (!res.ok) {
            var err = new Error(data.error || data.message || data.Message || ("HTTP " + res.status));
            err.status = res.status;
            throw err;
          }
          return data;
        });
      });
    }
    var pre = tokenExpired() && sessionStorage.getItem(SK_REFRESH) ? refreshToken() : Promise.resolve();
    return pre.then(doFetch).catch(function (err) {
      if ((err.status === 401 || err.status === 403) && sessionStorage.getItem(SK_REFRESH)) {
        return refreshToken().then(doFetch);
      }
      if (err.status === 401) signOut("登录已失效，请重新登录");
      throw err;
    });
  }

  window.Api = {
    request: request,
    get: function (path, params) { return request("GET", path, params); },
    put: function (path, body) { return request("PUT", path, null, body); },
    post: function (path, body) { return request("POST", path, null, body); },
  };

  // ---- 登录界面 ----
  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") el.className = attrs[k]; else el.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { el.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return el;
  }

  function injectStyles() {
    var css =
      "#authOverlay{position:fixed;inset:0;z-index:99999;background:#0f1117;display:flex;align-items:center;justify-content:center;" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;color:#e6e8ee}" +
      "#authCard{width:380px;max-width:92vw;background:#171a22;border:1px solid #2a2f3a;border-radius:12px;padding:26px 24px;box-shadow:0 12px 40px rgba(0,0,0,.5)}" +
      "#authCard h2{margin:0 0 4px;font-size:18px}#authCard .sub{color:#9aa3b2;font-size:12px;margin-bottom:18px}" +
      "#authCard label{display:block;font-size:12px;color:#9aa3b2;margin:12px 0 5px}" +
      "#authCard input{width:100%;padding:10px 11px;background:#1e222c;border:1px solid #2a2f3a;border-radius:7px;color:#e6e8ee;font-size:14px}" +
      "#authCard input:focus{outline:none;border-color:#4f9cff}" +
      "#authCard button.primary{width:100%;margin-top:18px;padding:11px;background:#4f9cff;color:#fff;border:none;border-radius:7px;font-size:14px;font-weight:600;cursor:pointer}" +
      "#authCard button.primary:hover{background:#3d8bef}#authCard button.primary:disabled{opacity:.6;cursor:default}" +
      "#authCard .linkrow{margin-top:14px;text-align:center}#authCard a.link{color:#4f9cff;font-size:12px;cursor:pointer}" +
      "#authStatus{margin-top:14px;font-size:12px;min-height:16px;text-align:center;color:#9aa3b2;white-space:pre-wrap}" +
      "#authStatus.err{color:#f85149}#authStatus.ok{color:#3fb950}" +
      "#authCard ol{margin:6px 0 0;padding-left:18px;font-size:12px;color:#9aa3b2;line-height:1.7}" +
      "#authCard .secret{margin-top:10px;padding:10px;background:#0b0d12;border:1px dashed #4f9cff;border-radius:7px;" +
      "font-family:ui-monospace,Menlo,monospace;font-size:14px;letter-spacing:1px;word-break:break-all;text-align:center}" +
      "#authCard .row2{display:flex;gap:8px;justify-content:center;margin-top:8px}" +
      "#authCard .row2 a,#authCard .row2 button{font-size:12px;color:#4f9cff;background:none;border:1px solid #2a2f3a;" +
      "border-radius:6px;padding:4px 10px;cursor:pointer;text-decoration:none}";
    document.head.appendChild(h("style", {}, [css]));
  }

  function setStatus(msg, kind) { if (statusEl) { statusEl.textContent = msg || ""; statusEl.className = kind || ""; } }
  function card() {
    if (!overlay || !overlay.parentNode) {
      overlay = h("div", { id: "authOverlay" }, [h("div", { id: "authCard" }, [])]);
      document.body.appendChild(overlay);
    }
    overlay.style.display = "";
    var c = document.getElementById("authCard");
    c.innerHTML = "";
    return c;
  }
  function onEnter(input, fn) { input.addEventListener("keydown", function (e) { if (e.key === "Enter") fn(); }); }

  function showLogin(msg) {
    var c = card();
    c.appendChild(h("h2", {}, ["Connect Customer 队列监控"]));
    c.appendChild(h("div", { class: "sub" }, ["请登录后使用 / Sign in to continue"]));
    var email = h("input", { type: "email", autocomplete: "username" });
    email.value = pendingEmail;
    var pass = h("input", { type: "password", autocomplete: "current-password" });
    var btn = h("button", { class: "primary" }, ["登录"]);
    var forgot = h("a", { class: "link" }, ["忘记密码？"]);
    [h("label", {}, ["邮箱"]), email, h("label", {}, ["密码"]), pass, btn, h("div", { class: "linkrow" }, [forgot])]
      .forEach(function (el) { c.appendChild(el); });
    statusEl = h("div", { id: "authStatus" }, []);
    c.appendChild(statusEl);
    if (msg) setStatus(msg, "err");

    function submit() {
      if (!email.value.trim() || !pass.value) { setStatus("请输入邮箱和密码。", "err"); return; }
      pendingEmail = email.value.trim();
      btn.disabled = true;
      setStatus("正在登录…");
      idp("InitiateAuth", {
        AuthFlow: "USER_PASSWORD_AUTH", ClientId: CFG.clientId,
        AuthParameters: { USERNAME: pendingEmail, PASSWORD: pass.value },
      }).then(handleAuthResponse)
        .catch(function (e) { setStatus(e.message || "登录失败", "err"); btn.disabled = false; });
    }
    btn.addEventListener("click", submit);
    onEnter(pass, submit);
    forgot.addEventListener("click", function () { pendingEmail = email.value.trim(); showForgot(); });
    (pendingEmail ? pass : email).focus();
  }

  function showNewPassword() {
    var c = card();
    c.appendChild(h("h2", {}, ["首次登录，请设置新密码"]));
    c.appendChild(h("div", { class: "sub" }, [pendingEmail + " · 至少 12 位，含大小写字母和数字"]));
    var p1 = h("input", { type: "password", autocomplete: "new-password" });
    var p2 = h("input", { type: "password", autocomplete: "new-password" });
    var btn = h("button", { class: "primary" }, ["提交"]);
    [h("label", {}, ["新密码"]), p1, h("label", {}, ["确认新密码"]), p2, btn].forEach(function (el) { c.appendChild(el); });
    statusEl = h("div", { id: "authStatus" }, []);
    c.appendChild(statusEl);
    function submit() {
      if (!p1.value) { setStatus("请输入新密码。", "err"); return; }
      if (p1.value !== p2.value) { setStatus("两次输入的密码不一致。", "err"); return; }
      btn.disabled = true;
      setStatus("提交中…");
      idp("RespondToAuthChallenge", {
        ClientId: CFG.clientId, ChallengeName: "NEW_PASSWORD_REQUIRED", Session: pendingSession,
        ChallengeResponses: { USERNAME: pendingEmail, NEW_PASSWORD: p1.value },
      }).then(handleAuthResponse)
        .catch(function (e) { setStatus(e.message || "设置新密码失败", "err"); btn.disabled = false; });
    }
    btn.addEventListener("click", submit);
    onEnter(p2, submit);
    p1.focus();
  }

  /** 统一处理 InitiateAuth / RespondToAuthChallenge 的返回: 拿到 Token 或进入下一个挑战 */
  function handleAuthResponse(d) {
    if (d.AuthenticationResult) { saveSession(d.AuthenticationResult); onAuthenticated(); return; }
    pendingSession = d.Session;
    if (d.ChallengeName === "NEW_PASSWORD_REQUIRED") { showNewPassword(); return; }
    if (d.ChallengeName === "MFA_SETUP") { showMfaSetup(); return; }
    if (d.ChallengeName === "SOFTWARE_TOKEN_MFA") { showMfaCode(); return; }
    throw new Error("暂不支持的认证流程: " + (d.ChallengeName || "?"));
  }

  function sessionError(e) {
    var m = (e && e.message) || "";
    return /session/i.test(m) ? "验证已超时(约 3 分钟)，请返回重新登录。" : (m || "验证失败");
  }

  function codeInput() {
    return h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6",
      placeholder: "6 位数字" });
  }

  /** 首次登录: 绑定验证器 App(TOTP) */
  function showMfaSetup() {
    var c = card();
    c.appendChild(h("h2", {}, ["绑定验证器 App"]));
    c.appendChild(h("div", { class: "sub" }, [pendingEmail + " · 系统要求多因素认证(MFA)"]));
    statusEl = h("div", { id: "authStatus" }, ["正在生成密钥…"]);
    c.appendChild(statusEl);
    idp("AssociateSoftwareToken", { Session: pendingSession }).then(function (d) {
      pendingSession = d.Session;
      var secret = d.SecretCode;
      c.innerHTML = "";
      c.appendChild(h("h2", {}, ["绑定验证器 App"]));
      c.appendChild(h("div", { class: "sub" }, [pendingEmail + " · 系统要求多因素认证(MFA)"]));
      var steps = h("ol", {}, []);
      ["在手机上打开验证器 App(Google Authenticator、Microsoft Authenticator、1Password 等)",
        "选择「输入设置密钥」，账号填你的邮箱，密钥填下方字符(基于时间)",
        "输入 App 上显示的 6 位验证码完成绑定"].forEach(function (t) { steps.appendChild(h("li", {}, [t])); });
      c.appendChild(steps);
      c.appendChild(h("div", { class: "secret", id: "mfaSecret" }, [secret.replace(/(.{4})/g, "$1 ").trim()]));
      var issuer = "ConnectMonitor";
      var uri = "otpauth://totp/" + encodeURIComponent(issuer + ":" + pendingEmail) +
        "?secret=" + encodeURIComponent(secret) + "&issuer=" + encodeURIComponent(issuer);
      var copy = h("button", { type: "button" }, ["复制密钥"]);
      copy.addEventListener("click", function () {
        (navigator.clipboard ? navigator.clipboard.writeText(secret) : Promise.reject())
          .then(function () { copy.textContent = "已复制"; })
          .catch(function () { copy.textContent = "请手动复制"; });
      });
      c.appendChild(h("div", { class: "row2" }, [copy, h("a", { href: uri }, ["在本机的验证器中打开"])]));
      var code = codeInput();
      var btn = h("button", { class: "primary" }, ["完成绑定"]);
      [h("label", {}, ["验证码"]), code, btn].forEach(function (el) { c.appendChild(el); });
      statusEl = h("div", { id: "authStatus" }, []);
      c.appendChild(statusEl);
      addBackLink(c);
      function submit() {
        var v = code.value.replace(/\s/g, "");
        if (!/^\d{6}$/.test(v)) { setStatus("请输入 6 位数字验证码。", "err"); return; }
        btn.disabled = true;
        setStatus("验证中…");
        idp("VerifySoftwareToken", { Session: pendingSession, UserCode: v, FriendlyDeviceName: "authenticator" })
          .then(function (r) {
            if (r.Status !== "SUCCESS") throw new Error("验证码不正确，请重试");
            return idp("RespondToAuthChallenge", {
              ClientId: CFG.clientId, ChallengeName: "MFA_SETUP", Session: r.Session,
              ChallengeResponses: { USERNAME: pendingEmail },
            });
          }).then(handleAuthResponse)
          .catch(function (e) { setStatus(sessionError(e), "err"); btn.disabled = false; });
      }
      btn.addEventListener("click", submit);
      onEnter(code, submit);
      code.focus();
    }).catch(function (e) { setStatus(sessionError(e), "err"); });
    addBackLink(c);
  }

  function addBackLink(c) {
    var back = h("a", { class: "link" }, ["返回登录"]);
    back.addEventListener("click", function () { showLogin(); });
    c.appendChild(h("div", { class: "linkrow" }, [back]));
  }

  /** 已绑定验证器: 输入 6 位验证码 */
  function showMfaCode() {
    var c = card();
    c.appendChild(h("h2", {}, ["输入验证码"]));
    c.appendChild(h("div", { class: "sub" }, [pendingEmail + " · 打开验证器 App 查看 6 位验证码"]));
    var code = codeInput();
    var btn = h("button", { class: "primary" }, ["验证"]);
    var back = h("a", { class: "link" }, ["返回登录"]);
    [h("label", {}, ["验证码"]), code, btn, h("div", { class: "linkrow" }, [back])].forEach(function (el) { c.appendChild(el); });
    statusEl = h("div", { id: "authStatus" }, []);
    c.appendChild(statusEl);
    function submit() {
      var v = code.value.replace(/\s/g, "");
      if (!/^\d{6}$/.test(v)) { setStatus("请输入 6 位数字验证码。", "err"); return; }
      btn.disabled = true;
      setStatus("验证中…");
      idp("RespondToAuthChallenge", {
        ClientId: CFG.clientId, ChallengeName: "SOFTWARE_TOKEN_MFA", Session: pendingSession,
        ChallengeResponses: { USERNAME: pendingEmail, SOFTWARE_TOKEN_MFA_CODE: v },
      }).then(handleAuthResponse)
        .catch(function (e) { setStatus(sessionError(e), "err"); btn.disabled = false; code.select(); });
    }
    btn.addEventListener("click", submit);
    onEnter(code, submit);
    back.addEventListener("click", function () { showLogin(); });
    code.focus();
  }

  function showForgot() {
    var c = card();
    c.appendChild(h("h2", {}, ["忘记密码"]));
    c.appendChild(h("div", { class: "sub" }, ["验证码将发送到你的邮箱"]));
    var email = h("input", { type: "email", autocomplete: "username" });
    email.value = pendingEmail;
    var send = h("button", { class: "primary" }, ["发送验证码"]);
    var wrap = h("div", { style: "display:none" }, []);
    var code = h("input", { type: "text", autocomplete: "one-time-code" });
    var np = h("input", { type: "password", autocomplete: "new-password" });
    var reset = h("button", { class: "primary" }, ["重置密码"]);
    [h("label", {}, ["验证码"]), code, h("label", {}, ["新密码"]), np, reset].forEach(function (el) { wrap.appendChild(el); });
    var back = h("a", { class: "link" }, ["返回登录"]);
    [h("label", {}, ["邮箱"]), email, send, wrap, h("div", { class: "linkrow" }, [back])].forEach(function (el) { c.appendChild(el); });
    statusEl = h("div", { id: "authStatus" }, []);
    c.appendChild(statusEl);
    send.addEventListener("click", function () {
      if (!email.value.trim()) { setStatus("请输入邮箱。", "err"); return; }
      pendingEmail = email.value.trim();
      send.disabled = true;
      idp("ForgotPassword", { ClientId: CFG.clientId, Username: pendingEmail }).then(function () {
        setStatus("验证码已发送，请查收邮件。", "ok");
        wrap.style.display = "block";
        send.textContent = "重新发送";
        send.disabled = false;
      }).catch(function (e) { setStatus(e.message || "发送失败", "err"); send.disabled = false; });
    });
    reset.addEventListener("click", function () {
      if (!code.value.trim() || !np.value) { setStatus("请输入验证码与新密码。", "err"); return; }
      reset.disabled = true;
      idp("ConfirmForgotPassword", {
        ClientId: CFG.clientId, Username: pendingEmail, ConfirmationCode: code.value.trim(), Password: np.value,
      }).then(function () {
        setStatus("密码已重置，请用新密码登录。", "ok");
        setTimeout(function () { showLogin(); }, 1200);
      }).catch(function (e) { setStatus(e.message || "重置失败", "err"); reset.disabled = false; });
    });
    back.addEventListener("click", function () { showLogin(); });
  }

  function revealApp() {
    var pre = document.getElementById("preAuthHide");
    if (pre) pre.parentNode.removeChild(pre);
    if (overlay) overlay.style.display = "none";
  }

  var booted = false;
  function onAuthenticated() {
    var email = CFG.mock ? "dev@example.com" : (emailFromToken(idToken) || pendingEmail);
    sessionStorage.setItem(SK_EMAIL, email);
    revealApp();
    if (window.MonitorApp) {
      if (!booted) { booted = true; window.MonitorApp.boot({ email: email }); }
      else window.MonitorApp.resume({ email: email });
    }
  }

  function signOut(msg) {
    clearSession();
    if (window.MonitorApp && window.MonitorApp.pause) window.MonitorApp.pause();
    if (!document.getElementById("preAuthHide")) {
      var st = document.createElement("style");
      st.id = "preAuthHide";
      st.textContent = "body > header, body > .layout { visibility: hidden; }";
      document.head.appendChild(st);
    }
    showLogin(typeof msg === "string" ? msg : "");
  }
  window.Auth = { signOut: signOut };

  function start() {
    injectStyles();
    if (CFG.mock) { onAuthenticated(); return; }
    if (!CFG.clientId || !CFG.region) {
      card().appendChild(h("div", { id: "authStatus", class: "err" }, ["缺少部署配置(aws-config.js)，无法登录。"]));
      return;
    }
    var tok = sessionStorage.getItem(SK_TOKEN);
    if (tok && !sessionTooOld()) {
      idToken = tok;
      if (!tokenExpired()) { onAuthenticated(); return; }
      if (sessionStorage.getItem(SK_REFRESH)) {
        refreshToken().then(onAuthenticated).catch(function () { showLogin(); });
        return;
      }
    }
    clearSession();
    showLogin();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else setTimeout(start, 0); // 保证 app.js 先定义 window.MonitorApp
})();
