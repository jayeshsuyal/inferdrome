import {
  Activity,
  Fingerprint,
  GitCompareArrows,
  LayoutDashboard,
  Layers3,
  Moon,
  Route,
  Sun,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { PropsWithChildren } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useRuns } from "../context/RunsContext";
import { useDashboardAuth } from "../context/DashboardAuthContext";
import { formatDateTime } from "../lib/format";
import { NavLink, useLocation, useParams } from "../lib/router";

type Theme = "light" | "dark";

function initialTheme(): Theme {
  const stored = window.localStorage.getItem("inferdrome-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function pathLabel(pathname: string): string {
  if (/^\/evaluations\/.+/.test(pathname)) return "Evidence / Evaluation report";
  if (pathname.startsWith("/evaluations")) return "Evidence / Evaluations";
  if (/^\/routing-executions\/.+/.test(pathname)) return "Evidence / Routing execution detail";
  if (pathname.startsWith("/routing-executions")) return "Evidence / Routing executions";
  if (/^\/routing-qualifications\/.+/.test(pathname)) return "Evidence / Causal qualification detail";
  if (pathname.startsWith("/routing-qualifications")) return "Evidence / Causal qualification";
  if (/^\/routing-campaigns\/.+/.test(pathname)) return "Evidence / Routing campaign detail";
  if (pathname.startsWith("/routing-campaigns")) return "Evidence / Routing campaigns";
  if (/^\/trial-sets\/.+/.test(pathname)) return "Evidence / Trial set detail";
  if (pathname.startsWith("/trial-sets")) return "Evidence / Trial sets";
  if (/^\/comparisons\/.+/.test(pathname)) return "Evidence / Comparison detail";
  if (pathname.startsWith("/comparisons")) return "Evidence / Controlled comparisons";
  if (pathname.startsWith("/compare")) return "Evidence / Compare two runs";
  if (pathname.startsWith("/evidence")) return "Evidence / Bundle";
  if (/^\/runs\/.+/.test(pathname)) return "Evidence / Run detail";
  return "Evidence / Runs";
}

interface NavigationItem {
  readonly label: string;
  readonly to: string | null;
  readonly icon: LucideIcon;
  readonly end?: boolean;
  readonly activeOn?: readonly string[];
}

export function AppShell({ children }: PropsWithChildren) {
  const location = useLocation();
  const { runs, rejected, generatedAt, status, refresh } = useRuns();
  const { runId } = useParams();
  const { authRequired, token, clear } = useDashboardAuth();
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const mainRef = useRef<HTMLElement>(null);
  const previousPath = useRef(location.pathname);
  const selectedRunId = runId ?? runs[0]?.run_id ?? null;

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem("inferdrome-theme", theme);
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    if (themeColor) themeColor.content = theme === "dark" ? "#0e110f" : "#f3f5f2";
  }, [theme]);

  useEffect(() => {
    if (previousPath.current === location.pathname) return;
    previousPath.current = location.pathname;
    mainRef.current?.focus({ preventScroll: true });
  }, [location.pathname]);

  const navigation = useMemo<readonly NavigationItem[]>(
    () => [
      { label: "Runs", to: "/runs", icon: LayoutDashboard, end: true },
      { label: "Trial sets", to: "/trial-sets", icon: Layers3 },
      { label: "Evaluations", to: "/evaluations", icon: Activity },
      { label: "Routing campaigns", to: "/routing-campaigns", icon: Route },
      { label: "Routing executions", to: "/routing-executions", icon: Fingerprint },
      { label: "Causal qualification", to: "/routing-qualifications", icon: GitCompareArrows },
      {
        label: "Run detail",
        to: selectedRunId ? `/runs/${encodeURIComponent(selectedRunId)}` : null,
        icon: Activity,
      },
      {
        label: "Comparisons",
        to: "/comparisons",
        icon: GitCompareArrows,
        activeOn: ["/compare"],
      },
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
          Evidence source
          <strong>Local bundles</strong>
        </div>

        <nav className="primary-nav" aria-label="Dashboard views">
          {navigation.map(({ activeOn, label, to, icon: Icon, end }) =>
            to ? (
              <NavLink
                key={label}
                activeOn={activeOn}
                className="nav-link"
                to={to}
                end={end}
                onFocus={(event) => {
                  const nav = event.currentTarget.parentElement;
                  if (nav && nav.scrollWidth > nav.clientWidth
                    && ["auto", "scroll"].includes(getComputedStyle(nav).overflowX)) {
                    event.currentTarget.scrollIntoView({ block: "nearest", inline: "nearest" });
                  }
                }}
              >
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
            {status === "success" ? "Run snapshot loaded" : status === "error" ? "Run snapshot unavailable" : "Reading run snapshot"}
          </strong>
          <span>{indexLabel}</span>
        </div>
      </aside>

      <main className="main" id="dashboard-content" ref={mainRef} tabIndex={-1}>
        <div className="topline">
          <span className="topline-path">{pathLabel(location.pathname)}</span>
          <div className="topline-actions">
            {generatedAt ? <span className="index-time">Runs last verified {formatDateTime(generatedAt)}</span> : null}
            <button className="button button-secondary" type="button" onClick={refresh} disabled={status === "loading"}>
              Refresh runs
            </button>
            {authRequired && token !== null ? (
              <button className="button button-secondary lock-button" type="button" onClick={clear}>
                Lock dashboard
              </button>
            ) : null}
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
