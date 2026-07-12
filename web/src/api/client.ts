import createClient from "openapi-fetch";

import type { paths } from "./schema";

// The frozen contract declares `servers: [{ url: / }]` (same-origin), so the
// client's default base URL is empty and Vite proxies the API paths to the
// backend in dev (see vite.config.ts). VITE_API_BASE overrides it for setups
// where the API is served elsewhere.
export const apiBaseUrl = import.meta.env.VITE_API_BASE ?? "";

// Auth is a placeholder (DESIGN §23 / contract bearerAuth): the local
// single-principal deployment accepts any token. VITE_API_TOKEN overrides it.
// Exported so the SSE reader (which uses fetch, not this client, because
// EventSource can't send an Authorization header) sends the same credential.
export const apiToken = import.meta.env.VITE_API_TOKEN ?? "local-dev";

export const api = createClient<paths>({ baseUrl: apiBaseUrl });

api.use({
  onRequest({ request }) {
    request.headers.set("Authorization", `Bearer ${apiToken}`);
    return request;
  },
});
