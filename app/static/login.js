const form = document.getElementById("loginForm");
const error = document.getElementById("loginError");
const button = document.getElementById("loginBtn");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  error.hidden = true;
  button.disabled = true;
  button.textContent = "Signing in…";
  try {
    const r = await fetch(new URL("api/login", location.origin + location.pathname), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.username.value, password: form.password.value }),
    });
    if (r.ok) {
      location.replace("./" + (location.hash || ""));
      return;
    }
    const data = await r.json().catch(() => ({}));
    error.textContent = data.detail || `Sign-in failed (HTTP ${r.status})`;
  } catch {
    error.textContent = "Cannot reach Update Manager.";
  }
  error.hidden = false;
  button.disabled = false;
  button.textContent = "Sign in";
  form.password.select();
});
