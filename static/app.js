(() => {
  "use strict";

  // Intentionally module-private and ephemeral: never persisted to browser storage.
  let accessToken = null;
  let publicConfig = null;

  const loginView = document.querySelector("#login-view");
  const profileView = document.querySelector("#profile-view");
  const loginForm = document.querySelector("#login-form");
  const loginButton = document.querySelector("#login-button");
  const loginStatus = document.querySelector("#login-status");
  const profileStatus = document.querySelector("#profile-status");
  const profileOutput = document.querySelector("#profile-output");
  const logoutButton = document.querySelector("#logout-button");
  const emailInput = document.querySelector("#email");
  const passwordInput = document.querySelector("#password");

  function setLoggedIn(loggedIn) {
    loginView.classList.toggle("hidden", loggedIn);
    profileView.classList.toggle("hidden", !loggedIn);
  }

  function clearSession(message = "") {
    accessToken = null;
    profileOutput.textContent = "";
    profileStatus.textContent = "";
    loginStatus.textContent = message;
    setLoggedIn(false);
  }

  async function loadConfig() {
    const response = await fetch("/api/config", {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit"
    });
    if (!response.ok) {
      throw new Error("configuration unavailable");
    }
    const config = await response.json();
    if (!config.supabaseUrl || !config.supabasePublishableKey) {
      throw new Error("configuration invalid");
    }
    return config;
  }

  async function loadProfile() {
    const response = await fetch("/api/me", {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${accessToken}`
      },
      cache: "no-store",
      credentials: "omit"
    });
    if (response.status === 401) {
      clearSession("Your session was not accepted. Please sign in again.");
      return;
    }
    if (!response.ok) {
      profileStatus.textContent = "The profile service is temporarily unavailable.";
      return;
    }
    const profile = await response.json();
    // textContent prevents profile fields from being interpreted as markup.
    profileOutput.textContent = JSON.stringify(profile.user, null, 2);
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginStatus.textContent = "Signing in…";
    loginButton.disabled = true;

    try {
      publicConfig ||= await loadConfig();
      const response = await fetch(
        `${publicConfig.supabaseUrl}/auth/v1/token?grant_type=password`,
        {
          method: "POST",
          headers: {
            Accept: "application/json",
            apikey: publicConfig.supabasePublishableKey,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            email: emailInput.value,
            password: passwordInput.value
          }),
          cache: "no-store",
          credentials: "omit"
        }
      );

      if (!response.ok) {
        throw new Error("sign-in rejected");
      }
      const session = await response.json();
      if (!session.access_token) {
        throw new Error("missing access token");
      }

      accessToken = session.access_token;
      emailInput.value = "";
      passwordInput.value = "";
      loginStatus.textContent = "";
      setLoggedIn(true);
      await loadProfile();
    } catch (_error) {
      passwordInput.value = "";
      clearSession("Sign-in failed. Check your details or try again shortly.");
    } finally {
      loginButton.disabled = false;
    }
  });

  logoutButton.addEventListener("click", () => {
    clearSession("You have signed out.");
    emailInput.focus();
  });

  loadConfig()
    .then((config) => {
      publicConfig = config;
    })
    .catch(() => {
      loginStatus.textContent = "Sign-in is temporarily unavailable.";
      loginButton.disabled = true;
    });
})();
