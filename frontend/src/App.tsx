import { AppShell } from "./components/AppShell";
import { EmptyState, PageHeader } from "./components/Primitives";
import { RunsProvider } from "./context/RunsContext";
import { Navigate, useLocation } from "./lib/router";
import { CompareView } from "./views/CompareView";
import { EvidenceView } from "./views/EvidenceView";
import { RunDetailView } from "./views/RunDetailView";
import { RunsView } from "./views/RunsView";

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
  const { pathname } = useLocation();
  let view;
  if (pathname === "/") view = <Navigate to="/runs" replace />;
  else if (pathname === "/runs" || pathname === "/runs/") view = <RunsView />;
  else if (/^\/runs\/[^/]+\/?$/.test(pathname)) view = <RunDetailView />;
  else if (pathname === "/compare" || pathname === "/compare/") view = <CompareView />;
  else if (pathname === "/evidence" || pathname === "/evidence/" || /^\/evidence\/[^/]+\/?$/.test(pathname)) {
    view = <EvidenceView />;
  } else view = <NotFoundView />;

  return (
    <RunsProvider>
      <AppShell>{view}</AppShell>
    </RunsProvider>
  );
}
