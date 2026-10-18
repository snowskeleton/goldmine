// Passkey (WebAuthn) helpers and small page utilities. No dependencies.

const b64u = {
  decode(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    const bin = atob(s + "===".slice((s.length + 3) % 4));
    return Uint8Array.from(bin, c => c.charCodeAt(0)).buffer;
  },
  encode(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

async function postJSON(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
                                 body: JSON.stringify(body || {}), credentials: "same-origin" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function creationOptions(o) {
  o.challenge = b64u.decode(o.challenge);
  o.user.id = b64u.decode(o.user.id);
  (o.excludeCredentials || []).forEach(c => { c.id = b64u.decode(c.id); });
  return o;
}

function requestOptions(o) {
  o.challenge = b64u.decode(o.challenge);
  (o.allowCredentials || []).forEach(c => { c.id = b64u.decode(c.id); });
  return o;
}

function registrationJSON(cred) {
  const r = cred.response;
  return {
    id: cred.id, rawId: b64u.encode(cred.rawId), type: cred.type,
    response: { clientDataJSON: b64u.encode(r.clientDataJSON), attestationObject: b64u.encode(r.attestationObject),
                transports: r.getTransports ? r.getTransports() : [] },
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
  };
}

function authenticationJSON(cred) {
  const r = cred.response;
  return {
    id: cred.id, rawId: b64u.encode(cred.rawId), type: cred.type,
    response: { clientDataJSON: b64u.encode(r.clientDataJSON), authenticatorData: b64u.encode(r.authenticatorData),
                signature: b64u.encode(r.signature), userHandle: r.userHandle ? b64u.encode(r.userHandle) : null },
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
  };
}

function showError(scope, err) {
  const el = scope.querySelector(".error") || scope.parentElement.querySelector(".error");
  if (!el) return alert(err.message || err);
  el.textContent = err.name === "NotAllowedError" ? "The passkey prompt was cancelled or timed out." : (err.message || String(err));
  el.hidden = false;
}

function noPasskeys(scope) {
  if (window.PublicKeyCredential) return false;
  showError(scope, new Error("This browser doesn't support passkeys. Use a current Safari, Chrome, Edge or Firefox over HTTPS (or localhost)."));
  return true;
}

const registerForm = document.getElementById("register");
if (registerForm) {
  registerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (noPasskeys(registerForm)) return;
    const button = registerForm.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const f = new FormData(registerForm);
      const mode = registerForm.dataset.mode;
      const options = await postJSON("/auth/register/options",
        { name: f.get("name") || "", mode, invite: registerForm.dataset.invite || null });
      const cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
      const result = await postJSON("/auth/register/verify",
        { credential: registrationJSON(cred), passkeyName: f.get("passkeyName") || "", mode });
      location.href = result.redirect || "/";
    } catch (err) {
      showError(registerForm, err);
      button.disabled = false;
    }
  });
}

const signin = document.getElementById("signin");
if (signin) {
  signin.addEventListener("click", async () => {
    const scope = signin.parentElement;
    if (noPasskeys(scope)) return;
    signin.disabled = true;
    try {
      const options = await postJSON("/auth/login/options");
      const cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
      const result = await postJSON("/auth/login/verify", { credential: authenticationJSON(cred) });
      location.href = result.redirect || "/";
    } catch (err) {
      showError(scope, err);
      signin.disabled = false;
    }
  });
}

function copyField(id, button) {
  const field = document.getElementById(id);
  field.select();
  navigator.clipboard.writeText(field.value).then(() => {
    const label = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = label; }, 1500);
  });
}

function filterRows(input, tableId) {
  const q = input.value.toLowerCase();
  document.querySelectorAll(`#${tableId} tr`).forEach((tr, i) => {
    if (i > 0) tr.hidden = q && !tr.textContent.toLowerCase().includes(q);
  });
}
