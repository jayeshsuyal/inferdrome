import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { setDashboardToken } from "../lib/api";

interface DashboardAuthContextValue {
  readonly token: string | null;
  readonly authRequired: boolean;
  readonly authVersion: number;
  readonly authError: string | null;
  readonly unlock: (token: string) => void;
  readonly clear: () => void;
  readonly noteUnauthorized: () => void;
}

const defaultValue: DashboardAuthContextValue = {
  token: null,
  authRequired: false,
  authVersion: 0,
  authError: null,
  unlock: () => undefined,
  clear: () => undefined,
  noteUnauthorized: () => undefined,
};

const DashboardAuthContext = createContext<DashboardAuthContextValue>(defaultValue);

export function DashboardAuthProvider({ children }: { readonly children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [authVersion, setAuthVersion] = useState(0);
  const [authError, setAuthError] = useState<string | null>(null);

  const unlock = useCallback((nextToken: string) => {
    setDashboardToken(nextToken);
    setToken(nextToken);
    setAuthRequired(true);
    setAuthError(null);
    setAuthVersion((value) => value + 1);
  }, []);

  const clear = useCallback(() => {
    setDashboardToken(null);
    setToken(null);
    setAuthRequired(true);
    setAuthError(null);
    setAuthVersion((value) => value + 1);
  }, []);

  const noteUnauthorized = useCallback(() => {
    setDashboardToken(null);
    setToken(null);
    setAuthRequired(true);
    setAuthError("The dashboard token was not accepted. Paste an active local token and try again.");
  }, []);

  const value = useMemo<DashboardAuthContextValue>(
    () => ({ token, authRequired, authVersion, authError, unlock, clear, noteUnauthorized }),
    [authError, authRequired, authVersion, clear, noteUnauthorized, token, unlock],
  );
  return <DashboardAuthContext.Provider value={value}>{children}</DashboardAuthContext.Provider>;
}

export function useDashboardAuth(): DashboardAuthContextValue {
  return useContext(DashboardAuthContext);
}
