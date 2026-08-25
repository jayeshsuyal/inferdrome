import { AppShell } from "./components/AppShell";
import { DashboardUnlockScreen } from "./components/DashboardUnlockScreen";
import { EmptyState, PageHeader } from "./components/Primitives";
import { useDashboardAuth } from "./context/DashboardAuthContext";
import { RunsProvider } from "./context/RunsContext";
import { Navigate, useLocation } from "./lib/router";
import { CompareView } from "./views/CompareView";
import { ControlledComparisonDetailView } from "./views/ControlledComparisonDetailView";
import { ControlledComparisonsView } from "./views/ControlledComparisonsView";
import { EvidenceView } from "./views/EvidenceView";
import { RunDetailView } from "./views/RunDetailView";
import { RunsView } from "./views/RunsView";
import { TrialSetDetailView } from "./views/TrialSetDetailView";
import { TrialSetsView } from "./views/TrialSetsView";

function NotFoundView() {
  return (
    <>
      <PageHeader
        title="View not found"
        subtitle="That dashboard route does not exist in this Inferdrome build."
      />
      <EmptyState title="Nothing is indexed here" message="Return to Runs to inspect the available evidence bundles." />
    </>
  );
}

export function App() {
  const { authRequired, token } = useDashboardAuth();
  const { pathname } = useLocation();
  let view;
  if (pathname === "/") view = <Navigate to="/runs" replace />;
  else if (pathname === "/runs" || pathname === "/runs/") view = <RunsView />;
  else if (/^\/runs\/[^/]+\/?$/.test(pathname)) view = <RunDetailView />;
  else if (pathname === "/trial-sets" || pathname === "/trial-sets/") view = <TrialSetsView />;
  else if (/^\/trial-sets\/[^/]+\/?$/.test(pathname)) view = <TrialSetDetailView />;
  else if (pathname === "/comparisons" || pathname === "/comparisons/") view = <ControlledComparisonsView />;
  else if (/^\/comparisons\/[^/]+\/?$/.test(pathname)) view = <ControlledComparisonDetailView />;
  else if (pathname === "/compare" || pathname === "/compare/") view = <CompareView />;
  else if (pathname === "/evidence" || pathname === "/evidence/" || /^\/evidence\/[^/]+\/?$/.test(pathname)) {
    view = <EvidenceView />;
  } else view = <NotFoundView />;

  return (
    <RunsProvider>
      {authRequired && token === null ? <DashboardUnlockScreen /> : <AppShell>{view}</AppShell>}
    </RunsProvider>
  );
}
