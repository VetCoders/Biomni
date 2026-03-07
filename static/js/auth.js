import { apiFetch, clearToken, setToken } from "./api.js";

function normalizeUser(user) {
  if (!user || typeof user !== "object") {
    return { display_name: "Scientist" };
  }

  return {
    id: user.id || user.user_id || "",
    display_name: user.display_name || user.name || user.email || "Scientist",
    email: user.email || "",
  };
}

function extractToken(payload) {
  if (!payload || typeof payload !== "object") return "";
  return payload.token || payload.access_token || payload.jwt || "";
}

export function createAuth({
  modal,
  closeButton,
  errorEl,
  loginTab,
  registerTab,
  loginForm,
  registerForm,
  onAuthenticated,
  onLoggedOut,
}) {
  let activeTab = "login";
  let locked = true;

  function switchTab(tab) {
    activeTab = tab === "register" ? "register" : "login";

    loginTab.classList.toggle("active", activeTab === "login");
    registerTab.classList.toggle("active", activeTab === "register");

    loginForm.classList.toggle("active", activeTab === "login");
    registerForm.classList.toggle("active", activeTab === "register");

    hideError();
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove("hidden");
  }

  function hideError() {
    errorEl.textContent = "";
    errorEl.classList.add("hidden");
  }

  function setLocked(nextLocked) {
    locked = Boolean(nextLocked);
    closeButton.classList.toggle("hidden", locked);
  }

  function show(tab = "login", options = {}) {
    setLocked(options.locked !== undefined ? options.locked : true);
    switchTab(tab);
    modal.classList.remove("hidden");
    document.body.classList.add("auth-open");
  }

  function hide() {
    if (locked) return;
    modal.classList.add("hidden");
    document.body.classList.remove("auth-open");
    hideError();
  }

  function forceHide() {
    modal.classList.add("hidden");
    document.body.classList.remove("auth-open");
    hideError();
  }

  async function authenticate(endpoint, body) {
    hideError();

    const payload = await apiFetch(endpoint, {
      method: "POST",
      body: JSON.stringify(body),
    });

    const token = extractToken(payload);
    if (!token) {
      throw new Error("Authentication token missing in response.");
    }

    setToken(token);

    let user = payload.user ? normalizeUser(payload.user) : null;
    if (!user) {
      const me = await apiFetch("/api/auth/me");
      user = normalizeUser(me?.user || me);
    }

    onAuthenticated({ token, user });
    forceHide();
  }

  loginTab.addEventListener("click", () => switchTab("login"));
  registerTab.addEventListener("click", () => switchTab("register"));

  closeButton.addEventListener("click", () => {
    if (!locked) hide();
  });

  modal.addEventListener("click", (event) => {
    if (event.target === modal && !locked) {
      hide();
    }
  });

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const formData = new FormData(loginForm);
    const email = String(formData.get("email") || "").trim();
    const password = String(formData.get("password") || "");

    if (!email || !password) {
      showError("Email and password are required.");
      return;
    }

    try {
      await authenticate("/api/auth/login", { email, password });
    } catch (error) {
      showError(error.message || "Login failed.");
    }
  });

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const formData = new FormData(registerForm);
    const displayName = String(formData.get("display_name") || "").trim();
    const email = String(formData.get("email") || "").trim();
    const password = String(formData.get("password") || "");

    if (!displayName || !email || !password) {
      showError("Display name, email, and password are required.");
      return;
    }

    try {
      await authenticate("/api/auth/register", {
        display_name: displayName,
        email,
        password,
      });
    } catch (error) {
      showError(error.message || "Registration failed.");
    }
  });

  return {
    show,
    hide,
    forceHide,
    setLocked,
    logout() {
      clearToken();
      onLoggedOut();
      show("login", { locked: true });
    },
  };
}
