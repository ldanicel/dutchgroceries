// Source of the "Connect helper" bookmarklet. Served with the __PLACEHOLDERS__ filled in and
// collapsed to one line, so: one statement per line, semicolons, no trailing // comments.
(async () => {
  const APP = "__APP__";
  const banner = (text, ok) => {
    const d = document.createElement("div");
    d.textContent = text;
    d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:14px;font:600 16px system-ui,sans-serif;text-align:center;color:#fff;background:" + (ok ? "#1baf7a" : "#c96a00");
    document.body.appendChild(d);
  };
  const send = (provider, code, state, verifier) => {
    banner("Got it. Handing the login to groceries…", true);
    location.href = APP + "/connect/callback?provider=" + provider + "#code=" + encodeURIComponent(code) + (state ? "&state=" + encodeURIComponent(state) : "") + (verifier ? "&verifier=" + encodeURIComponent(verifier) : "");
  };
  const host = location.hostname;
  if (host === "auth.jumbo.com") {
    const shown = document.querySelector("input[name=code]");
    if (shown && shown.value) { const st = document.querySelector("input[name=state]"); send("jumbo", shown.value, st && st.value, null); return; }
    const b64u = (buf) => btoa(String.fromCharCode.apply(null, new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const verifier = b64u(crypto.getRandomValues(new Uint8Array(48)));
    const challenge = b64u(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    const q = new URLSearchParams({client_id: "__JUMBO_CLIENT__", redirect_uri: "__JUMBO_REDIRECT__", audience: "__JUMBO_AUDIENCE__", scope: "__JUMBO_SCOPE__", response_type: "code", response_mode: "form_post", platform: "mobile", code_challenge: challenge, code_challenge_method: "S256", state: b64u(crypto.getRandomValues(new Uint8Array(16))), nonce: b64u(crypto.getRandomValues(new Uint8Array(16)))});
    const codeIn = (html) => /name=["']code["'][^>]*value=["']([^"']+)/i.exec(html);
    let res = null;
    let page = "";
    try { res = await fetch("/authorize?" + q.toString(), {credentials: "include"}); page = await res.text(); } catch (e) { page = ""; }
    let m = codeIn(page);
    if (!m && res && res.url.indexOf("/u/consent") !== -1) {
      const form = new DOMParser().parseFromString(page, "text/html").querySelector("form");
      const fields = new URLSearchParams();
      if (form) { form.querySelectorAll("input[name]").forEach((i) => fields.append(i.name, i.value)); }
      fields.set("action", "accept");
      const target = form && form.getAttribute("action") ? new URL(form.getAttribute("action"), res.url).toString() : res.url;
      try { page = await (await fetch(target, {method: "POST", credentials: "include", headers: {"Content-Type": "application/x-www-form-urlencoded"}, body: fields.toString()})).text(); } catch (e) { page = ""; }
      m = codeIn(page);
    }
    if (m) { send("jumbo", m[1], null, verifier); return; }
    if (res && res.url.indexOf("/u/login") !== -1) { banner("Groceries helper: you are not logged in to Jumbo in this browser yet. Log in, then click the helper again.", false); return; }
    const err = /name=["']error["'][^>]*value=["']([^"']+)/i.exec(page);
    const desc = /name=["']error_description["'][^>]*value=["']([^"']+)/i.exec(page);
    banner("Groceries helper: Jumbo did not hand out a code (" + (err ? err[1] + (desc ? ": " + desc[1] : "") : "no session") + "). Use 'Paste instead' in the dialog with the red 'Failed to launch' line from the Console.", false);
    return;
  }
  if (host === "login.ah.nl" || host.endsWith(".ah.nl")) {
    const findCode = (text) => { const m = /login-exit[^"'<>\s]{0,40}?code=([A-Za-z0-9._~%-]+)/.exec(String(text || "")); return m ? decodeURIComponent(m[1]) : null; };
    const grab = (url) => { const c = /^appie:/.test(String(url || "")) ? findCode(url) : null; if (c) { send("ah", c, null, null); return true; } return false; };
    let code = findCode(document.documentElement.outerHTML);
    let landed = "";
    if (!code) {
      try {
        const res = await fetch("https://login.ah.nl/secure/oauth/authorize?client_id=appie&redirect_uri=appie://login-exit&response_type=code", {credentials: "include"});
        landed = res.url;
        code = findCode(await res.text());
        if (!code) { code = findCode(await (await fetch(res.url, {credentials: "include", headers: {"RSC": "1"}})).text()); }
      } catch (e) { landed = ""; }
    }
    if (code) { send("ah", code, null, null); return; }
    if (window.navigation) {
      window.navigation.addEventListener("navigate", (e) => { if (grab(e.destination.url)) e.preventDefault(); });
    }
    const assign = window.location.assign.bind(window.location);
    try { window.location.assign = (u) => { if (!grab(u)) assign(u); }; } catch (e) { }
    document.addEventListener("click", (e) => { const a = e.target.closest && e.target.closest("a[href^='appie:']"); if (a && grab(a.href)) e.preventDefault(); }, true);
    if (/ingelogd/i.test(landed)) {
      banner("Groceries helper: AH says you are logged in but did not hand out a code here. Copy the red 'Failed to launch appie://…' line from F12 → Console into 'Paste instead'.", false);
    } else {
      banner("Groceries helper: you are not logged in to AH in this browser yet. Log in now (the helper is watching), or log in and click it again.", false);
    }
    return;
  }
  alert("Groceries connect helper: use it on the Albert Heijn or Jumbo login tab that the app opened.");
})();
