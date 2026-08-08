import {
  Activity,
  Fingerprint,
  GitCompareArrows,
  LayoutDashboard,
  Moon,
  Sun,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { PropsWithChildren } from "react";
import { useEffect, useMemo, useState } from "react";

import { useRuns } from "../context/RunsContext";
import { formatDateTime } from "../lib/format";
import { NavLink, useLocation } from "../lib/router";

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const stored = window.localStorage.getItem("inferdrome-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function selectedRunFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/(?:runs|evidence)\/([^/]+)$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function pathLabel(pathname: string): string {
  if (pathname.startsWith("/compare")) return "Evidence / Compare";
  if (pathname.startsWith("/evidence")) return "Evidence / Bundle";
  if (/^\/runs\/.+/.test(pathname)) return "Evidence / Run detail";
  return "Evidence / Runs";
}

interface NavigationItem {
  readonly label: string;
  readonly to: string | null;
  readonly icon: LucideIcon;
  readonly end?: boolean;
}

export function AppShell({ children }: PropsWithChildren) {
  const location = useLocation();
  const { runs, rejected, generatedAt, status } = useRuns();
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const selectedRunId = selectedRunFromPath(location.pathname) || runs[0]?.run_id || null;

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem("inferdrome-theme", theme);
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    if (themeColor) themeColor.content = theme === "dark" ? "#0e110f" : "#f3f5f2";
  }, [theme]);

  const navigation = useMemo<readonly NavigationItem[]>(
    () => [
      { label: "Runs", to: "/runs", icon: LayoutDashboard, end: true },
      {
        label: "Run detail",
        to: selectedRunId ? `/runs/${encodeURIComponent(selectedRunId)}` : null,
        icon: Activity,
      },
      { label: "Compare", to: "/compare", icon: GitCompareArrows },
      {
        label: "Evidence",
        to: selectedRunId ? `/evidence/${encodeURIComponent(selectedRunId)}` : null,
        icon: Fingerprint,
      },
    ],
    [selectedRunId],
  );

  const indexLabel =
    status === "loading"
      ? "Reading evidence index"
      : status === "error"
        ? "Evidence index unavailable"
        : `${runs.length} verified · ${rejected.length} rejected`;

  return (
    <div className="app-frame">
      <a className="skip-link" href="#dashboard-content">
        Skip to dashboard content
      </a>
      <aside className="sidebar">
        <NavLink className="brand" to="/runs" aria-label="Inferdrome runs">
          <span className="brand-mark" aria-hidden="true" />
          <span>Inferdrome</span>
        </NavLink>

        <div className="workspace-label">
          Evidence root
          <strong>./runs</strong>
        </div>

        <nav className="primary-nav" aria-label="Dashboard views">
          {navigation.map(({ label, to, icon: Icon, end }) =>
            to ? (
              <NavLink key={label} className="nav-link" to={to} end={end}>
                <Icon aria-hidden="true" />
                <span>{label}</span>
              </NavLink>
            ) : (
              <span key={label} className="nav-link nav-link-disabled" aria-disabled="true">
                <Icon aria-hidden="true" />
                <span>{label}</span>
              </span>
            ),
          )}
        </nav>

        <div className="sidebar-state">
          <strong>
            <span className={`index-dot index-${status}`} aria-hidden="true" />
            {status === "success" ? "Index current" : status === "error" ? "Index unavailable" : "Indexing"}
          </strong>
          <span>{indexLabel}</span>
        </div>
      </aside>

      <main className="main" id="dashboard-content">
        <div className="topline">
          <span className="topline-path">{pathLabel(location.pathname)}</span>
          <div className="topline-actions">
            {generatedAt ? <span className="index-time">Indexed {formatDateTime(generatedAt)}</span> : null}
            <button
              className="icon-button"
              type="button"
              onClick={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
              title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            >
              {theme === "dark" ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
            </button>
          </div>
        </div>
        {children}
      </main>
    </div>
  );
}
