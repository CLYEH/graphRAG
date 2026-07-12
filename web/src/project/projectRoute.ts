import { useParams } from "react-router-dom";

// A project key is any non-empty string (frozen contract), so it can hold URL-
// reserved chars, dot segments (`.`/`..` — which browsers normalize away before
// the router sees them), or unicode. Rather than patch each hazard, encode the
// key into an OPAQUE base64url segment: its alphabet is [A-Za-z0-9_-], which
// contains no URL-reserved char and no dot segment, so EVERY key round-trips and
// there is no residual encoding surface (Codex #65, three encoding rounds).

export function encodeProjectSegment(key: string): string {
  const bytes = new TextEncoder().encode(key);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function decodeProjectSegment(segment: string): string | undefined {
  try {
    const b64 = segment.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const bin = atob(padded);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    // fatal:true so bytes that aren't valid UTF-8 throw here rather than
    // decoding to U+FFFD — a malformed segment must normalize to one "unknown"
    // signal (undefined), never a lossy garbage key.
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return undefined; // a malformed segment is an unknown project, not a crash
  }
}

// The active project key, decoded from the `:project` route segment.
export function useActiveProject(): string | undefined {
  const { project } = useParams<{ project: string }>();
  return project === undefined ? undefined : decodeProjectSegment(project);
}

// Whether a key can ride in a REST path segment (e.g. /projects/{key}/health).
// The app route base64url-encodes the key so any key is *openable*, but a REST
// call must send the literal key through `encodeURIComponent`, and exactly two
// families survive that as an un-addressable segment — this is the complete set,
// derived from the transport, not an open-ended spelling chase:
//   1. "." / ".." — `encodeURIComponent` leaves them as bare dot tokens, which
//      the URL parser normalizes away before routing (verified: "." ->
//      /projects/health, ".." -> /health; %2e/%2E normalize too, so no encoding
//      survives).
//   2. keys containing "/" — encoded to %2F, which the ASGI server decodes back
//      to "/" before routing, so the single-segment {project} route (not
//      {project:path}) misses it (verified: GET /projects/a%2Fb/health -> 404).
// Every other char percent-encodes to a non-dot %XX that decodes to itself and
// the segment survives, so query/hash chars, spaces, unicode and "%" all round-
// trip. These keys are openable but not API-addressable without a contract
// change; callers must refuse rather than silently hit the wrong endpoint.
export function isPathAddressable(key: string): boolean {
  return key !== "." && key !== ".." && !key.includes("/");
}
