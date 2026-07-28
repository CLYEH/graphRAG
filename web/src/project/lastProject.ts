// QA9/D17: the root used to land on `projects[0]`, and `list_projects` orders
// by created_at DESC — so an operator opening the Console dropped into the
// NEWEST project, which is typically the empty shell someone just created,
// not the one they were working in. Remember the last project they actually
// opened instead.
//
// The stored key is a HINT, never an authority: `resolveLandingProject` only
// honours it when the key is still in the fetched list, so a project deleted
// (or renamed, or opened on a machine whose storage is stale) falls back to
// the list rather than routing the operator into a 404 shell.

const KEY = "graphrag.lastProject";

// localStorage is absent in SSR/tests and THROWS on access under Safari
// private mode and some enterprise policies — a Console that cannot render
// because a convenience feature could not read storage is a worse bug than
// the one this fixes, so every access degrades to "no preference".
function storage(): Storage | undefined {
  try {
    return typeof localStorage === "undefined" ? undefined : localStorage;
  } catch {
    return undefined;
  }
}

export function readLastProject(): string | undefined {
  try {
    return storage()?.getItem(KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

export function writeLastProject(key: string): void {
  try {
    storage()?.setItem(KEY, key);
  } catch {
    // quota exceeded / storage disabled — the preference is best-effort
  }
}

/**
 * Which project the root should open, given what the API returned.
 *
 * Kept as a pure function (rather than inline in the component) so the
 * precedence is testable without a router: remembered-and-still-present wins,
 * otherwise the list's own first entry, and an empty list has no answer.
 */
export function resolveLandingProject(
  projects: readonly { name: string }[] | undefined,
): string | undefined {
  if (!projects || projects.length === 0) return undefined;
  const remembered = readLastProject();
  if (remembered !== undefined && projects.some((p) => p.name === remembered)) return remembered;
  return projects[0].name;
}
