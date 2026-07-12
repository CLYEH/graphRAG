import createClient from "openapi-fetch";

import type { paths } from "./schema";

// The frozen contract declares `servers: [{ url: / }]` (same-origin), so the
// client's default base URL is empty and Vite proxies the API paths to the
// backend in dev (see vite.config.ts). VITE_API_BASE overrides it for setups
// where the API is served elsewhere.
const baseUrl = import.meta.env.VITE_API_BASE ?? "";

// Auth is a placeholder (DESIGN §23 / contract bearerAuth): the local
// single-principal deployment accepts any token. VITE_API_TOKEN overrides it.
const token = import.meta.env.VITE_API_TOKEN ?? "local-dev";

export const api = createClient<paths>({ baseUrl });

api.use({
  onRequest({ request }) {
    request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
});
