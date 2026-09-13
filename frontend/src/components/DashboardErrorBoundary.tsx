import { Component, type ReactNode } from "react";

export class DashboardErrorBoundary extends Component<
  { readonly children: ReactNode },
  { readonly failed: boolean }
> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  private retry = () => this.setState({ failed: false });

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;

    return (
      <main className="auth-screen" aria-labelledby="dashboard-recovery-title">
        <section className="auth-card" role="alert">
          <p className="eyebrow">Local dashboard</p>
          <h1 id="dashboard-recovery-title">This view could not be displayed</h1>
          <p className="auth-copy">Try again to reload the local evidence for this view.</p>
          <button className="button auth-submit" type="button" onClick={this.retry} autoFocus>
            Try again
          </button>
        </section>
      </main>
    );
  }
}
