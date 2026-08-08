import type {
  AnchorHTMLAttributes,
  MouseEvent,
  PropsWithChildren,
} from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

interface NavigateOptions {
  readonly replace?: boolean;
}

type Navigate = (to: string, options?: NavigateOptions) => void;

interface RouterValue {
  readonly pathname: string;
  readonly navigate: Navigate;
}

const RouterContext = createContext<RouterValue | null>(null);

function internalPath(to: string): string | null {
  const base = typeof window === "undefined" ? "http://inferdrome.local" : window.location.origin;
  const url = new URL(to, base);
  if (url.origin !== base) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

function useRouter(): RouterValue {
  const router = useContext(RouterContext);
  if (!router) throw new Error("Inferdrome router context is missing");
  return router;
}

export function BrowserRouter({ children }: PropsWithChildren) {
  const [pathname, setPathname] = useState(() => window.location.pathname);

  useEffect(() => {
    const handlePopState = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigate = useCallback<Navigate>((to, options) => {
    const path = internalPath(to);
    if (!path) return;
    window.history[options?.replace ? "replaceState" : "pushState"](null, "", path);
    setPathname(window.location.pathname);
    window.scrollTo({ top: 0, behavior: "instant" });
  }, []);

  const value = useMemo(() => ({ pathname, navigate }), [navigate, pathname]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

interface MemoryRouterProps extends PropsWithChildren {
  readonly initialEntries?: readonly string[];
}

export function MemoryRouter({ children, initialEntries = ["/"] }: MemoryRouterProps) {
  const [pathname, setPathname] = useState(() => internalPath(initialEntries[0] ?? "/")?.split(/[?#]/, 1)[0] ?? "/");
  const navigate = useCallback<Navigate>((to) => {
    const path = internalPath(to);
    if (path) setPathname(path.split(/[?#]/, 1)[0]);
  }, []);
  const value = useMemo(() => ({ pathname, navigate }), [navigate, pathname]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

interface LinkProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> {
  readonly to: string;
}

export function Link({ children, onClick, target, to, ...props }: LinkProps) {
  const { navigate } = useRouter();
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      (target && target !== "_self") ||
      !internalPath(to)
    ) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };
  return <a {...props} href={to} target={target} onClick={handleClick}>{children}</a>;
}

interface NavLinkProps extends LinkProps {
  readonly end?: boolean;
}

export function NavLink({ className, end = false, to, ...props }: NavLinkProps) {
  const { pathname } = useRouter();
  const destination = internalPath(to)?.split(/[?#]/, 1)[0] ?? "";
  const active = end
    ? pathname === destination
    : pathname === destination || pathname.startsWith(`${destination}/`);
  const classes = [className, active ? "active" : null].filter(Boolean).join(" ");
  return <Link {...props} className={classes || undefined} to={to} aria-current={active ? "page" : undefined} />;
}

export function Navigate({ replace = false, to }: NavigateOptions & { readonly to: string }) {
  const navigate = useNavigate();
  useEffect(() => navigate(to, { replace }), [navigate, replace, to]);
  return null;
}

export function useLocation(): { readonly pathname: string } {
  const { pathname } = useRouter();
  return useMemo(() => ({ pathname }), [pathname]);
}

export function useNavigate(): Navigate {
  return useRouter().navigate;
}

export function useParams(): { readonly runId?: string } {
  const { pathname } = useRouter();
  const match = pathname.match(/^\/(?:runs|evidence)\/([^/]+)\/?$/);
  if (!match) return {};
  try {
    return { runId: decodeURIComponent(match[1]) };
  } catch {
    return { runId: match[1] };
  }
}
