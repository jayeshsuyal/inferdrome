import { useEffect, useState, type FormEvent } from "react";

import { useDashboardAuth } from "../context/DashboardAuthContext";

export function DashboardUnlockScreen() {
  const { authError, unlock } = useDashboardAuth();
  const [token, setToken] = useState("");

  useEffect(() => {
    if (authError) setToken("");
  }, [authError]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    unlock(token);
  }

  return (
    <main className="auth-screen" aria-labelledby="dashboard-unlock-title">
      <div className="auth-card">
        <span className="brand-mark" aria-hidden="true" />
        <p className="eyebrow">Local dashboard</p>
        <h1 id="dashboard-unlock-title">Unlock evidence views</h1>
        <p className="auth-copy">
          Paste the local read-only dashboard token. It stays in memory and is forgotten when this page reloads.
        </p>
        <form onSubmit={submit}>
          <label htmlFor="dashboard-token">Bearer token</label>
          <input
            id="dashboard-token"
            name="dashboard-token"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            aria-describedby={authError ? "dashboard-auth-error" : undefined}
            autoFocus
          />
          {authError ? (
            <p id="dashboard-auth-error" className="auth-error" role="alert">
              {authError}
            </p>
          ) : null}
          <button className="button auth-submit" type="submit" disabled={!token}>
            Unlock dashboard
          </button>
        </form>
      </div>
    </main>
  );
}
