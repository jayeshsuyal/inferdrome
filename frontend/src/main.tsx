import "@fontsource-variable/instrument-sans/wght.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { DashboardAuthProvider } from "./context/DashboardAuthContext";
import { BrowserRouter } from "./lib/router";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Inferdrome dashboard root is missing");

createRoot(root).render(
  <StrictMode>
    <DashboardAuthProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </DashboardAuthProvider>
  </StrictMode>,
);
