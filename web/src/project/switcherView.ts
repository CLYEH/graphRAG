// QA9/D17: what the project switcher offers, as ONE decision.
//
// Split out of lastProject.ts because that module answers "which project do
// we open" while this one answers "which projects do we offer" — related
// questions, but someone looking for switcher logic would not grep a file
// named lastProject.

/** A project as the switcher needs it (the DTO carries more). */
export type SwitchableProject = { name: string; display_name?: string | null };

// The switcher offers a filter only once scanning is the slower option; below
// that a native select is fine and an extra control is noise.
export const FILTER_THRESHOLD = 8;

/**
 * What the switcher should show, as one decision.
 *
 * The threshold gates the filter's USE, not merely the rendering of its input.
 * Gating only the input left a typed filter applied while its control was
 * unmounted: projects shrink out-of-band (DELETE /projects, the CLI — the SPA
 * has no delete entry) and refetchOnWindowFocus re-renders on alt-tab, so the
 * operator returned to a silently narrowed list with no control to clear it.
 * Deriving `filtering` once and feeding every branch from it makes that state
 * unrepresentable rather than merely unlikely.
 */
export function switcherView(
  projects: readonly SwitchableProject[] | undefined,
  filter: string,
  active: string | undefined,
): { filtering: boolean; options: readonly SwitchableProject[]; noMatch: boolean } {
  const all = projects ?? [];
  const filtering = all.length >= FILTER_THRESHOLD;
  const needle = filtering ? filter.trim().toLowerCase() : "";

  const matches =
    needle === ""
      ? all
      : all.filter(
          (p) =>
            p.name.toLowerCase().includes(needle) ||
            (p.display_name ?? "").toLowerCase().includes(needle),
        );

  // The ACTIVE project stays listed even when it does not match: a select whose
  // value is absent from its options renders blank, which reads as "no project
  // open" rather than "your filter excluded it".
  const options =
    active !== undefined && !matches.some((p) => p.name === active)
      ? [...matches, ...all.filter((p) => p.name === active)]
      : matches;

  return { filtering, options, noMatch: filtering && needle !== "" && matches.length === 0 };
}
