import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "@/app/App";
import { retryDelay } from "@/api/polling";
import { lazyPage, Page } from "@/app/lazyPage";
import { NativeShell } from "@/native/NativeShell";
import { isNativeApp } from "@/native/platform";
import "@/styles/tokens.css";
import "@/styles/base.css";
import "@/styles/app.css";

const client = new QueryClient({
  defaultOptions: {
    // One retry after a random wait, so tabs do not retry together; a poll that keeps failing slows down (api/polling.ts, #536)
    queries: { retry: 1, retryDelay: attempt => retryDelay(attempt), refetchOnWindowFocus: true },
  },
});

// Video wall mode is its own page: no shell, no menus, signed in with a token. Loaded only on the wall, not with everyone's first screen (#537).
const WallApp = lazyPage(() => import("@/features/wall/WallApp"), "WallApp");
const base = import.meta.env.BASE_URL.replace(/\/$/, "");
const isWall = window.location.pathname === `${base}/wall` || window.location.pathname.startsWith(`${base}/wall/`);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <NativeShell>
      <QueryClientProvider client={client}>
        {isWall ? <Page><WallApp /></Page> : <App />}
      </QueryClientProvider>
    </NativeShell>
  </StrictMode>,
);

// Installable app: the worker serves the application shell only (see app/api/web_interface.py).
// The native app keeps nothing on the phone, so it has no worker and no cached shell.
if (import.meta.env.PROD && "serviceWorker" in navigator && !isNativeApp()) {
  window.addEventListener("load", () => void navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => undefined));
}
