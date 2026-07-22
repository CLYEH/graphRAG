import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef } from "react";

import { api } from "./client";
import { isPathAddressable } from "../project/projectRoute";

import type { components } from "./schema";

export type Project = components["schemas"]["Project"];
export type Source = components["schemas"]["Source"];
export type HealthReport = components["schemas"]["HealthReport"];
export type Build = components["schemas"]["Build"];
export type BuildStep = components["schemas"]["BuildStep"];
export type BuildStepItem = components["schemas"]["BuildStepItem"];
export type JobAccepted = components["schemas"]["JobAccepted"];
export type Job = components["schemas"]["Job"];
export type MergeCandidate = components["schemas"]["MergeCandidate"];
export type MergeCandidateStatus = components["schemas"]["MergeCandidateStatus"];
export type OntologyProposal = components["schemas"]["OntologyProposal"];
export type OntologyProposalStatus = components["schemas"]["OntologyProposalStatus"];
export type Document = components["schemas"]["Document"];
export type Chunk = components["schemas"]["Chunk"];
export type QueryMode = components["schemas"]["QueryMode"];
export type QueryResult = components["schemas"]["QueryResult"];

// Lists every project for the switcher. The switcher must reach any project,
// so page through meta.next_cursor to exhaustion rather than showing only the
// first page (the API caps a page at 500). Project counts in a local console
// are small; a searchable picker is the answer if that ever stops holding.
export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const all: Project[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects", {
          params: { query: { limit: 500, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

// The project's MCP connection surface (contract v1.3, DR-012 gateway). The
// server DERIVES the url from the gateway's own host/port settings and path
// shape, so the Console never builds it client-side — an operator copying this
// link must reach where the gateway actually serves. Same path-addressability
// gate as health: a non-addressable name cannot reach the route at all (which
// is exactly why the contract can promise a non-null url).
export function useMcpInfo(project: string | undefined) {
  return useQuery({
    queryKey: ["mcp-info", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const { data, error } = await api.GET("/projects/{project}/mcp", {
        params: { path: { project: project as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

// Project Health home (DESIGN §19): status light + counts + drift for the
// active build. `project` is the decoded key from the route; the query stays
// disabled until it resolves (a malformed segment never hits the API) and while
// the key is not path-addressable ("." / ".." would normalize to the wrong
// endpoint — the page reports that instead). Errors throw so the page fails loud
// (a health page that blanks hides the outage it exists to report).
export function useHealth(project: string | undefined) {
  return useQuery({
    queryKey: ["health", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const { data, error } = await api.GET("/projects/{project}/health", {
        params: { path: { project: project as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

// Build (run) history for the pipeline dashboard (DESIGN §19). Same project
// path-addressability gate as health; pages through next_cursor so old runs are
// reachable, and fails loud so a store outage shows rather than an empty table.
export function useBuilds(project: string | undefined) {
  return useQuery({
    queryKey: ["builds", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: Build[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/builds", {
          params: { path: { project: project as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

// RB1 §27.7 drill-down: the build's pipeline steps (newest run first), read only
// when a row is expanded (buildId set). Pages through all steps — a build has a
// handful (6 §5 stages × its runs), so the loop terminates fast. Used to
// diagnose WHERE a failed build failed before offering "retry failed only".
export function useBuildSteps(project: string, buildId: string | undefined) {
  return useQuery({
    queryKey: ["buildSteps", project, buildId],
    enabled: isPathAddressable(project) && buildId !== undefined,
    queryFn: async () => {
      const all: BuildStep[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/builds/{build_id}/steps", {
          params: { path: { project, build_id: buildId as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

const STEP_ITEMS_PAGE = 100;

// The two "did not succeed" outcomes this failure-diagnosis drill-down surfaces.
// The drill-down is ALWAYS filtered to one of these: default verbosity records
// only failed/skipped, but `sampled`/`all` verbosity ALSO persists successes
// (status ∉ {failed,skipped}) and rows come back ordered by id, so an UNFILTERED
// page could be all successes and bury the very failures this view exists to
// show. A single `filter[status]` per query keeps diagnosis practical at every
// verbosity; skipped items are reached by re-querying with "skipped" — the
// caller's 失敗/跳過 selector (Codex #102).
export type ItemDiagnosisStatus = "failed" | "skipped";

// RB1 §27.7 drill-down: one step's recorded item outcomes for ONE diagnosis
// status, read when a step is expanded (stepId set). item_ref is the stable
// retry key that "retry failed only" re-enters. PAGINATED (not a cursor-
// exhausting loop): a step's items can be corpus-sized (thousands, or millions
// under `sampled`/`all` verbosity), so the caller renders a page + "load more"
// rather than downloading and retaining the whole chain (Codex #102).
export function useStepItems(
  project: string,
  buildId: string,
  stepId: string | undefined,
  status: ItemDiagnosisStatus,
) {
  return useInfiniteQuery({
    queryKey: ["stepItems", project, buildId, stepId, status],
    enabled: isPathAddressable(project) && stepId !== undefined,
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET(
        "/projects/{project}/builds/{build_id}/steps/{step_id}/items",
        {
          params: {
            path: { project, build_id: buildId, step_id: stepId as string },
            // filter[status]=<status> (deepObject) — the endpoint's one allowed
            // facet; never send an unadopted sort/filter (400 loud, SS1a).
            query: { limit: STEP_ITEMS_PAGE, cursor: pageParam, filter: { status } },
          },
        },
      );
      if (error) throw new Error(error.error.message);
      return { rows: data.data, next: data.meta.next_cursor ?? undefined };
    },
    getNextPageParam: (last: { rows: BuildStepItem[]; next?: string }) => last.next,
  });
}

// RB1-retry: opens a NEW build reprocessing a terminal `failed` build's failed
// items (parent_build_id lineage; §27.7). 202 returns the job envelope; the
// child build is created at accept time, so builds/health are invalidated to
// surface it. `idempotencyKey` is a per-logical-attempt random key (the
// trigger/eval discipline): a retry after a lost 202 replays the ORIGINAL job
// instead of forking a second child or 409ing the still-running one.
export function useRetryBuild(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      buildId,
      idempotencyKey,
    }: {
      buildId: string;
      idempotencyKey: string;
    }) => {
      const { data, error } = await api.POST("/projects/{project}/builds/{build_id}/retry", {
        params: {
          path: { project, build_id: buildId },
          header: { "Idempotency-Key": idempotencyKey },
        },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["builds", project] });
      void queryClient.invalidateQueries({ queryKey: ["health", project] });
    },
  });
}

// Activates a READY build (DESIGN §14): flips builds.status under the partial
// unique index (DR-001 — at most one active). The server's preflight refuses a
// build without eval scores; that 400 is the caller's to render as guidance,
// not to pre-empt (the server owns §14, the UI only relays it). Idempotency
// mirrors useCancelJob: a per-logical-attempt random key, generated by the
// caller and reused across retries of the SAME attempt — deterministic keys
// would replay a stale stored response on a legitimate LATER re-activation
// (activation is not once-only: rollback can precede a re-activate). On
// success the health/builds reads are invalidated so every server-state
// projection (the Overview checklist above all) reflects the new world.
export function useActivateBuild(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      buildId,
      idempotencyKey,
    }: {
      buildId: string;
      idempotencyKey: string;
    }) => {
      const { data, error } = await api.POST("/projects/{project}/builds/{build_id}/activate", {
        params: {
          path: { project, build_id: buildId },
          header: { "Idempotency-Key": idempotencyKey },
        },
      });
      if (error) {
        // the §14 refusal's SUBSTANCE lives in details.failures (the gate
        // strings) — error.message is a generic "activation preflight failed
        // for build <uuid>" line; throwing only the message would render the
        // exact kind of dead end Track 4 exists to remove (Codex #77 R2)
        const details = error.error.details as { failures?: unknown } | null | undefined;
        const failures = Array.isArray(details?.failures)
          ? details.failures.filter((f): f is string => typeof f === "string")
          : [];
        throw new Error(failures.length > 0 ? failures.join("\n") : error.error.message);
      }
      return data.data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["health", project] });
      void queryClient.invalidateQueries({ queryKey: ["builds", project] });
    },
  });
}

// Runs the project's golden set against a NAMED build as an async job (UXC2a,
// over the UXC1a eval endpoint): 202 returns the job envelope, progress rides
// the job SSE like the triggers. `idempotencyKey` is a per-logical-attempt
// random key (the trigger/cancel discipline) — the server hashes it with the
// build path AND the golden-set fingerprint, so a retry after a lost 202
// replays the ORIGINAL job id instead of 409ing against the still-running job
// or double-running a finished eval. Deliberately NO invalidation here: the
// report lands in builds.eval only when the JOB completes, so the caller's
// terminal-state watcher owns the builds/health refresh (invalidating at
// accept time would refetch a world the eval has not changed yet).
export function useRunEval(project: string) {
  return useMutation({
    mutationFn: async ({
      buildId,
      idempotencyKey,
    }: {
      buildId: string;
      idempotencyKey: string;
    }) => {
      const { data, error } = await api.POST("/projects/{project}/builds/{build_id}/eval", {
        params: {
          path: { project, build_id: buildId },
          header: { "Idempotency-Key": idempotencyKey },
        },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

// Current job state (DESIGN §27.7). `jobId` is the user-pasted id; the query is
// disabled until one is entered. The live SSE stream (useJobStream) overlays the
// fast-moving fields on top of this; this fetch supplies the static ones (kind,
// build_id, timestamps, error) and the initial snapshot.
export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ["job", jobId],
    enabled: jobId !== null && jobId !== "",
    queryFn: async () => {
      const { data, error } = await api.GET("/jobs/{job_id}", {
        params: { path: { job_id: jobId as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

// Requests cancellation of a job (DESIGN §22). `idempotencyKey` is a per-logical-
// attempt random key (same lost-2xx class as the triggers): a retry after a lost
// response replays the stored cancellation instead of re-posting against a job
// whose state has moved on. On success the job snapshot is refetched so the
// status reflects the request even if the stream has closed.
export function useCancelJob(jobId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (idempotencyKey: string) => {
      const { data, error } = await api.POST("/jobs/{job_id}/cancel", {
        params: {
          path: { job_id: jobId as string },
          header: { "Idempotency-Key": idempotencyKey },
        },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["job", jobId] }),
  });
}

// Merge candidates awaiting entity-resolution review (DESIGN §17). Active-build
// scoped like the other project reads; pages through next_cursor so the whole
// queue is reachable, and fails loud so a store outage / no-active-build surfaces
// (with the API's message) rather than an empty queue that reads as "nothing to
// review". The server returns only still-reviewable rows (pending + deferred —
// api/routers/review.py keeps the list identical to §19's pending_review gauge;
// decided rows stay in the SoR for audit but never ride this endpoint).
export function useMergeCandidates(project: string | undefined) {
  return useQuery({
    queryKey: ["merge-candidates", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: MergeCandidate[] = [];
      let cursor: string | undefined;
      // The endpoint re-resolves the active build per request, so a build
      // activated mid-pagination would splice page 1 (old build) with later
      // pages (new build) — a mixed queue whose stale rows then 404 on decide.
      // Pin the first page's build_id and fail loud on a swap; a retry pulls a
      // clean single-build snapshot. (`undefined` = not yet seen; build_id itself
      // is string | null.)
      let buildId: string | null | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/merge-candidates", {
          params: { path: { project: project as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        if (buildId === undefined) buildId = data.meta.build_id;
        else if (data.meta.build_id !== buildId)
          throw new Error("The active build changed while loading the review queue — retry.");
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

export type ReviewVerb = "approve" | "reject" | "defer";

// Records a curator decision on a merge candidate (DESIGN §17). The verb rides
// the URL (three frozen paths), not a body field, so switch on it to keep the
// typed path a literal — an accept-and-ignore body verb would defeat the codegen.
// On success the queue is invalidated so the row reflects the new status (and a
// terminal status disables further actions). `reason` is optional.
export function useDecideMergeCandidate(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      candidateId,
      verb,
      reason,
    }: {
      candidateId: string;
      verb: ReviewVerb;
      reason: string | null;
    }) => {
      // A deterministic key per (candidate, verb): if a response is lost after the
      // server commits the decision, the curator's retry replays the stored 200
      // instead of hitting an illegal-transition 400 on the now-decided candidate
      // (which would misreport a successful review as failed). The transition model
      // makes each (candidate, verb) a once-only operation, so this natural key is
      // stable across retries without extra state; the endpoints dedupe on it.
      const params = {
        path: { project, candidate_id: candidateId },
        header: { "Idempotency-Key": `${candidateId}:${verb}` },
      };
      const body = { reason };
      const res =
        verb === "approve"
          ? await api.POST("/projects/{project}/merge-candidates/{candidate_id}/approve", {
              params,
              body,
            })
          : verb === "reject"
            ? await api.POST("/projects/{project}/merge-candidates/{candidate_id}/reject", {
                params,
                body,
              })
            : await api.POST("/projects/{project}/merge-candidates/{candidate_id}/defer", {
                params,
                body,
              });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["merge-candidates", project] }),
  });
}

// GOV3-fe: the C3c ontology-proposal POOL (DESIGN §17 — LLM-observed types not in
// the configured ontology, awaiting review). NOT build-scoped (project-wide, one
// row per proposed type), so — unlike the merge queue — no build_id pin is needed.
// The endpoint's default queue is `proposed`; pass a status for the audit view
// (accepted/rejected). Paged to exhaustion: the pool is bounded (one per type),
// not corpus-sized.
export function useOntologyProposals(project: string | undefined, status?: OntologyProposalStatus) {
  return useQuery({
    queryKey: ["ontology-proposals", project, status ?? "proposed"],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: OntologyProposal[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/ontology-proposals", {
          params: {
            path: { project: project as string },
            // filter[status]=<status> (deepObject) only for the audit view; omit
            // for the default `proposed` queue (the server's default).
            query: status ? { limit: 200, cursor, filter: { status } } : { limit: 200, cursor },
          },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

export type ProposalVerb = "accept" | "reject";

// Records a curator decision on an ontology proposal (DESIGN §17: proposed →
// accepted|rejected, terminal — a re-decide 409s). Accept adds the type to
// projects.config.ontology (the config next build reads), so it invalidates the
// project cache too; every decision invalidates the pool + Health (the
// pending_ontology_proposals count). The verb rides the URL (keeps the typed path
// a literal); the deterministic `${id}:${verb}` Idempotency-Key replays a lost
// 200 rather than 409ing the now-decided proposal (the merge-decide discipline).
export function useDecideOntologyProposal(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      proposalId,
      verb,
      reason,
    }: {
      proposalId: string;
      verb: ProposalVerb;
      reason: string | null;
    }) => {
      const params = {
        path: { project, proposal_id: proposalId },
        header: { "Idempotency-Key": `${proposalId}:${verb}` },
      };
      const body = { reason };
      const res =
        verb === "accept"
          ? await api.POST("/projects/{project}/ontology-proposals/{proposal_id}/accept", {
              params,
              body,
            })
          : await api.POST("/projects/{project}/ontology-proposals/{proposal_id}/reject", {
              params,
              body,
            });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
    onSuccess: (_data, { verb }) => {
      queryClient.invalidateQueries({ queryKey: ["ontology-proposals", project] });
      queryClient.invalidateQueries({ queryKey: ["health", project] });
      // accept mutated the project's configured ontology — the config any reader
      // (Settings, the ontology editor) shows must refresh.
      if (verb === "accept") queryClient.invalidateQueries({ queryKey: ["project", project] });
    },
  });
}

// GOV2-fe-4: the entity/relation review LISTS (DESIGN §17), status-parameterized —
// `needs_review` is the review queue (the SAME lifecycle facet health.py counts as
// needs_review_*, keeping tab and gauge on identical rows), `rejected` is the
// decided view (GOV2-fe-4a: excluded rows awaiting a possible restore). Both are
// active-build scoped like the merge queue. INCREMENTAL pagination (Codex #105 P2:
// needs_review can be corpus-sized, so page-to-exhaustion would serialize hundreds
// of requests before first paint) — useInfiniteQuery with a load-more, the
// useStepItems discipline. The build_id pin rides the pageParam: page 1 records
// meta.build_id, every later page compares and fails loud on a swap (a spliced
// two-build list would 404 on decide). A live 載入更多 across a swap trips the
// pin; a FULL refetch (focus/invalidate/remount) recomputes params from the fresh
// page 1 (react-query v5 getNextPageParam re-threading) and pulls a clean
// snapshot directly.
const REVIEW_PAGE = 50;

export type ReviewListStatus = "needs_review" | "rejected";
type ReviewPageParam = { cursor: string; buildId: string | null } | undefined;

export function useEntityReviewList(project: string | undefined, status: ReviewListStatus) {
  return useInfiniteQuery({
    queryKey: ["entity-review", project, status],
    enabled: project !== undefined && isPathAddressable(project),
    initialPageParam: undefined as ReviewPageParam,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/projects/{project}/entities", {
        params: {
          path: { project: project as string },
          query: { limit: REVIEW_PAGE, cursor: pageParam?.cursor, filter: { status } },
        },
      });
      if (error) throw new Error(error.error.message);
      if (pageParam !== undefined && data.meta.build_id !== pageParam.buildId)
        throw new Error("The active build changed while loading the review queue — retry.");
      const buildId = pageParam?.buildId ?? data.meta.build_id;
      return {
        rows: data.data,
        next: data.meta.next_cursor ? { cursor: data.meta.next_cursor, buildId } : undefined,
      };
    },
    getNextPageParam: (last: { rows: Entity[]; next: ReviewPageParam }) => last.next,
  });
}

// Mirror of useEntityReviewList over /relations (which has no `q`/`total` —
// neither is needed here). The decide flow reuses useDecideReviewTarget
// (kind="relation"); the `["relation-review", project]` key it invalidates
// prefix-matches BOTH status views of this hook (pinned by
// useDecideReviewTarget.test.tsx).
export function useRelationReviewList(project: string | undefined, status: ReviewListStatus) {
  return useInfiniteQuery({
    queryKey: ["relation-review", project, status],
    enabled: project !== undefined && isPathAddressable(project),
    initialPageParam: undefined as ReviewPageParam,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/projects/{project}/relations", {
        params: {
          path: { project: project as string },
          query: { limit: REVIEW_PAGE, cursor: pageParam?.cursor, filter: { status } },
        },
      });
      if (error) throw new Error(error.error.message);
      if (pageParam !== undefined && data.meta.build_id !== pageParam.buildId)
        throw new Error("The active build changed while loading the review queue — retry.");
      const buildId = pageParam?.buildId ?? data.meta.build_id;
      return {
        rows: data.data,
        next: data.meta.next_cursor ? { cursor: data.meta.next_cursor, buildId } : undefined,
      };
    },
    getNextPageParam: (last: { rows: Relation[]; next: ReviewPageParam }) => last.next,
  });
}

//: the two ACTIVE-relation quality facets #109 exposed on /relations — CLOSED
//: values (`confidence=low`, `evidence=missing`), predicate shared with the
//: §19 gauges via LOW_CONFIDENCE_BELOW (single-source, api/routers/inspect.py)
export type RelationGapFacet = "confidence" | "evidence";

// The gap lists (GOV2-fe-5): ACTIVE relations flagged by a quality facet.
// The facet is ORTHOGONAL to lifecycle — gauge parity comes from the
// COMBINATION `filter[status]=active` + facet (#109 gate-2: a fetch missing
// either half counts a different population than the Health gauge links
// from). Keyed INSIDE the "relation-review" family (status slot `gap-…`) so
// useDecideReviewTarget's existing prefix invalidation refreshes these lists
// after a decision with no new invalidation wiring.
export function useRelationGapList(project: string | undefined, facet: RelationGapFacet) {
  return useInfiniteQuery({
    queryKey: ["relation-review", project, `gap-${facet}`],
    enabled: project !== undefined && isPathAddressable(project),
    initialPageParam: undefined as ReviewPageParam,
    queryFn: async ({ pageParam }) => {
      const { data, error } = await api.GET("/projects/{project}/relations", {
        params: {
          path: { project: project as string },
          query: {
            limit: REVIEW_PAGE,
            cursor: pageParam?.cursor,
            filter: {
              status: "active",
              ...(facet === "confidence" ? { confidence: "low" } : { evidence: "missing" }),
            },
          },
        },
      });
      if (error) throw new Error(error.error.message);
      if (pageParam !== undefined && data.meta.build_id !== pageParam.buildId)
        throw new Error("The active build changed while loading the list — retry.");
      const buildId = pageParam?.buildId ?? data.meta.build_id;
      return {
        rows: data.data,
        next: data.meta.next_cursor ? { cursor: data.meta.next_cursor, buildId } : undefined,
      };
    },
    getNextPageParam: (last: { rows: Relation[]; next: ReviewPageParam }) => last.next,
  });
}

export type ReviewTargetKind = "entity" | "relation";
export type ReviewTargetVerb = "approve" | "reject";

// Records a curator decision on an entity or relation (DESIGN §17). review.py
// appends to the ledger and latest-manual-wins resolves (§27.3). The
// Idempotency-Key is DETERMINISTIC per (target, verb) — the merge-decide
// discipline — so a lost-response retry replays the stored 200 instead of
// double-recording a second ledger entry for one logical decision (Codex #105).
// The review queue only surfaces `needs_review` rows and a decision removes the
// row, so each (target, verb) is decided at most once from the queue. The decided
// view (GOV2-fe-4a) is the exception: a restore there is a DELIBERATE re-decision
// in a possible reject→restore→reject cycle, where the deterministic key would
// replay an EARLIER cycle's stored response — so its caller passes a fresh random
// `idempotencyKey` per attempt (the activate/trigger discipline); queue callers
// omit it and get the deterministic default. Verb+kind ride the URL (four frozen
// paths), so switch to keep each a codegen literal. onSuccess invalidates the
// matching list family (both status views), Health (the needs_review gauge
// moves), and the target's detail cache (an open drawer reflects the new status).
export function useDecideReviewTarget(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      kind,
      targetId,
      verb,
      reason,
      idempotencyKey,
    }: {
      kind: ReviewTargetKind;
      targetId: string;
      verb: ReviewTargetVerb;
      reason: string | null;
      idempotencyKey?: string;
    }) => {
      const header = { "Idempotency-Key": idempotencyKey ?? `${targetId}:${verb}` };
      const body = { reason };
      const res =
        kind === "entity"
          ? verb === "approve"
            ? await api.POST("/projects/{project}/entities/{entity_id}/approve", {
                params: { path: { project, entity_id: targetId }, header },
                body,
              })
            : await api.POST("/projects/{project}/entities/{entity_id}/reject", {
                params: { path: { project, entity_id: targetId }, header },
                body,
              })
          : verb === "approve"
            ? await api.POST("/projects/{project}/relations/{relation_id}/approve", {
                params: { path: { project, relation_id: targetId }, header },
                body,
              })
            : await api.POST("/projects/{project}/relations/{relation_id}/reject", {
                params: { path: { project, relation_id: targetId }, header },
                body,
              });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
    onSuccess: (_data, { kind, targetId }) => {
      void queryClient.invalidateQueries({
        queryKey: [kind === "entity" ? "entity-review" : "relation-review", project],
      });
      void queryClient.invalidateQueries({ queryKey: ["health", project] });
      void queryClient.invalidateQueries({ queryKey: [kind, project, targetId] });
    },
  });
}

// --- decision-surface shared predicates (H20c; lesson-catalog class 17) ----------

type DecisionLockInputs = {
  /** the decide mutation observer — pending means a decision is in flight */
  decide: { isPending: boolean };
  /** the queue query the rows come from — refreshing or refetch-FAILED means
   *  the rows on screen may not be the server's truth */
  list?: { isFetching: boolean; isError: boolean };
  /** surface-specific extra lock terms (see-the-pair resolution, scope
   *  freeze, …) — composed, never a replacement for the core terms */
  extra?: readonly boolean[];
};

/** The ONE lock predicate every decision surface derives its affordances
 * from, so a new surface reuses it instead of re-deriving (and drifting).
 * `isError` must NEVER unlock (Codex #108 P1): a failed post-decision
 * refetch keeps the old rows on screen, clears `isFetching`, and sets
 * `isError` — a predicate without the error term re-enables the decided row
 * and an opposite verb would silently reverse the decision just made.
 * `isFetching` covers the stale-while-revalidate window after a decision
 * (Codex #106 P1d: a second decision there re-hits the now-terminal target
 * and 409s). Confirm-CANCEL buttons deliberately gate on `decide.isPending`
 * alone — backing out must stay possible during a refetch. */
export function useDecisionLock({ decide, list, extra = [] }: DecisionLockInputs): boolean {
  return (
    decide.isPending ||
    (list !== undefined && (list.isFetching || list.isError)) ||
    extra.some(Boolean)
  );
}

/** ONE idempotency key per LOGICAL operation, not per click (Codex #108 R2;
 * class 17's idem-key trilogy): `mint` creates the key on the first attempt
 * for a target and RETAINS it across failed retries — a lost-response retry
 * replays the stored 200 instead of appending a second record (whose newer
 * latest-wins timestamp could override an intervening decision). `clear` on
 * success, so a later cycle (reject → restore → reject) mints fresh — the
 * deterministic `${id}:${verb}` key would replay an EARLIER cycle's stored
 * response. Map-keyed by target id: a parent rendering many rows and a
 * per-row instance (one id) both work identically. */
export function useRestoreKeys(): { mint: (id: string) => string; clear: (id: string) => void } {
  const keys = useRef(new Map<string, string>());
  return useMemo(
    () => ({
      mint: (id: string) => {
        const existing = keys.current.get(id);
        if (existing !== undefined) return existing;
        const minted = crypto.randomUUID();
        keys.current.set(id, minted);
        return minted;
      },
      clear: (id: string) => {
        keys.current.delete(id);
      },
    }),
    [],
  );
}

// The graph invocation carried in the contract's `options` channel (DESIGN §27.6).
// The generated schema types `options` as an open object, so the shape the runtime
// GraphOptions model enforces (api/schemas.py, extra="forbid") is declared here.
export type GraphOptions = {
  template: "neighbors" | "path" | "subgraph";
  entity: string;
  other_entity?: string | null;
  hops?: number;
};

export interface QueryForm {
  mode: QueryMode;
  query: string;
  topK: number | null;
  options: GraphOptions | null;
}

// Builds the per-mode request body. The codegen types all five query endpoints
// with one permissive QueryRequest, but the runtime enforces per-mode
// `extra="forbid"` models that reject a field by its PRESENCE (model_fields_set),
// so keys are included conditionally, never sent as null: semantic/sql/global
// take query + optional top_k; graph takes query + options (no top_k); hybrid
// takes query, optional top_k, and optional options (omitted — never null — to
// skip the graph mode). `query` is required for every mode, graph included.
export function queryBody(form: QueryForm): components["schemas"]["QueryRequest"] {
  const body: components["schemas"]["QueryRequest"] = { query: form.query };
  if (form.mode === "graph") {
    if (form.options) body.options = form.options;
    return body;
  }
  if (form.topK !== null) body.top_k = form.topK;
  if (form.mode === "hybrid" && form.options) body.options = form.options;
  return body;
}

// Runs a query against the active build (DESIGN §21/§22). A query is a read-only
// RPC — no Idempotency-Key and no cache invalidation (nothing changes server-side)
// — run on submit. The mode rides the URL, so switch to keep the path a codegen
// literal. Errors (503 unconfigured / 409 no active build / 400 rejected field /
// 404) throw so the page fails loud; §22 degradation instead comes back 200 with
// warnings, which the caller renders from the returned QueryResult.
export function useRunQuery(project: string) {
  return useMutation({
    mutationFn: async (form: QueryForm) => {
      const params = { path: { project } };
      const body = queryBody(form);
      const res =
        form.mode === "semantic"
          ? await api.POST("/projects/{project}/query/semantic", { params, body })
          : form.mode === "sql"
            ? await api.POST("/projects/{project}/query/sql", { params, body })
            : form.mode === "global"
              ? await api.POST("/projects/{project}/query/global", { params, body })
              : form.mode === "graph"
                ? await api.POST("/projects/{project}/query/graph", { params, body })
                : await api.POST("/projects/{project}/query/hybrid", { params, body });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
  });
}

// ─── FE1 Import (DESIGN §5/§15, BA1b projects/sources + BA2e triggers) ───

// A project's registered sources, newest first. Same path-addressability gate as
// the other project reads; pages through next_cursor so every source is reachable,
// and fails loud so a store outage surfaces rather than an empty list that would
// read as "no sources yet".
export function useSources(project: string | undefined) {
  return useQuery({
    queryKey: ["sources", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const all: Source[] = [];
      let cursor: string | undefined;
      do {
        const { data, error } = await api.GET("/projects/{project}/sources", {
          params: { path: { project: project as string }, query: { limit: 200, cursor } },
        });
        if (error) throw new Error(error.error.message);
        all.push(...data.data);
        cursor = data.meta.next_cursor ?? undefined;
      } while (cursor);
      return all;
    },
  });
}

export interface NewProject {
  name: string;
  displayName: string;
  description: string;
}

// Creates a project. No client Idempotency-Key: `name` is the projects primary
// key, so a duplicate — including a lost-201 retry — 409s "already exists" and
// fails loud (the operator sees the project in the switcher and moves on). A
// name-derived header key was rejected on purpose: `ProjectCreate.name` allows any
// non-empty string (unicode, or >255 chars), which is not a valid HTTP header
// value, so keying on it would break creating exactly the names the contract
// permits (Codex #70); and a stable name key would additionally replay a stale 201
// across a delete-then-recreate of the same name within the key TTL. Optional text
// fields are omitted when blank. Invalidates the project list so the switcher and
// root redirect pick the new project up.
export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: NewProject) => {
      const body: components["schemas"]["ProjectCreate"] = { name: input.name };
      if (input.displayName.trim() !== "") body.display_name = input.displayName.trim();
      if (input.description.trim() !== "") body.description = input.description.trim();
      const { data, error } = await api.POST("/projects", { body });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["projects"] }),
  });
}

// Registers a source under a project. `kind` is one of the ingest-wired kinds
// (text/structured); `metadata` carries a structured source's table + pk_column
// (read_csv_rows requires them). `idempotencyKey` is a per-logical-attempt random
// key (NOT uri-derived — `uri` isn't unique server-side and a stable natural key
// would suppress an intentional re-registration): a retry after a lost 201 replays
// the stored response instead of minting a duplicate row, while a NEW attempt (the
// form contents changed) carries a fresh key so deliberate duplicates stay
// possible. Invalidates the source list so the new row appears.
export function useAddSource(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      uri: string;
      kind: string;
      // free bag per the contract (additionalProperties: true) — structured
      // sends strings, xlsx's extra_columns is a string list (SRC1)
      metadata?: Record<string, unknown>;
      idempotencyKey: string;
    }) => {
      const body: components["schemas"]["SourceCreate"] = { uri: input.uri, kind: input.kind };
      if (input.metadata) body.metadata = input.metadata;
      const { data, error } = await api.POST("/projects/{project}/sources", {
        params: {
          path: { project },
          header: { "Idempotency-Key": input.idempotencyKey },
        },
        body,
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sources", project] }),
  });
}

export type UploadResult = components["schemas"]["UploadResult"];

// Uploads document files into the project's managed corpus (UXC2b, over the
// contract-v1.2 upload endpoint): multipart files → a 201 with a PER-FILE
// accepted/rejected manifest — a refused extension/oversize/undecodable file is
// a STATED refusal row (with the server's reason), never a silent drop and
// never an HTTP error; whole-request refusals (415 not-multipart / 413 total
// size / 400 malformed / 409 idempotency conflict) throw with the server's
// message verbatim. The body is a real FormData: openapi-fetch's default
// serializer passes FormData through untouched and omits Content-Type so the
// browser sets the multipart boundary. The CAST below is the UXC1a codegen
// follow-up landing where TASKS.md UXC2b put it: `format: binary` compiles to
// `files: string[]` (openapi-typescript cannot express browser File objects),
// so the contract-correct FormData cannot satisfy the generated TYPE — the
// cast is confined to this one seam and the runtime shape is exactly what the
// contract freezes. On success the sources read is invalidated: accepted
// files register/update the project's ONE managed corpus source, which must
// appear in the existing list without a manual refresh.
export function useUploadDocuments(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      files,
      metadata,
      idempotencyKey,
    }: {
      files: File[];
      /** Per-file DocumentMetadataInput keyed by SUBMITTED filename — the
       *  endpoint's one metadata part. Only include files in this batch
       *  (orphan keys are a whole-request 400). */
      metadata?: Record<string, unknown>;
      idempotencyKey: string;
    }) => {
      const form = new FormData();
      for (const f of files) form.append("files", f);
      if (metadata && Object.keys(metadata).length > 0)
        form.append("metadata", JSON.stringify(metadata));
      const { data, error } = await api.POST("/projects/{project}/uploads", {
        params: { path: { project }, header: { "Idempotency-Key": idempotencyKey } },
        body: form as unknown as { files: string[] },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sources", project] }),
  });
}

export type TriggerKind = "ingest" | "build";

// Triggers a pipeline run (both kinds run the full pipeline; the kind rides the
// URL, switched to keep the typed path a codegen literal). The body is sent EMPTY:
// IngestRequest.source_ids and BuildRequest.reason are 400-rejected by presence
// until the pipeline honors them (BA2e-1). `idempotencyKey` is a per-logical-
// attempt random key: create_job_exclusive only dedups while the first job is
// non-terminal, so a retry after a LOST 202 would otherwise either 409 with no job
// id to watch (job still running) or double-trigger a second full pipeline (job
// finished) — with the key, the retry replays the stored 202 and hands back the
// ORIGINAL job id. Returns the accepted job so the caller can watch it live.
export function useTrigger(project: string) {
  return useMutation({
    mutationFn: async ({ kind, idempotencyKey }: { kind: TriggerKind; idempotencyKey: string }) => {
      const params = { path: { project }, header: { "Idempotency-Key": idempotencyKey } };
      const res =
        kind === "ingest"
          ? await api.POST("/projects/{project}/ingest", { params })
          : await api.POST("/projects/{project}/build", { params });
      if (res.error) throw new Error(res.error.error.message);
      return res.data.data;
    },
  });
}

// ---- FE3 Inspect (BA3 reads) ------------------------------------------------
//
// DESIGN §10.2 names this page 檢視(文件/chunks) — the ingested documents and the
// chunks they were split into. Entity/relation detail with evidence is the spec'd
// content of a DIFFERENT page (圖譜互動探索 / FE4: "點邊顯示 type/confidence/
// evidence/來源"), so the entity/relation reads are FE4's, not this task's.
//
// Three things the frozen contract makes non-negotiable here:
//
// 1. NEVER send `sort` or `filter[...]` that this client hasn't adopted. The
//    op params expose them, but `reject_unsupported_query`
//    (api/routers/_query.py) 400s any `filter[...]` outside an endpoint's
//    explicit allowlist (GOV4 merge-candidates `filter[status]`; SS1a inspect
//    facets: entities/relations `type|status|review_status`, documents
//    `status` — Console adoption is SS1b's Graph-page migration) and
//    any sort other than the list's own default — and for CHUNKS, whose default
//    order is the compound (document_id, ordinal), it rejects EVERY explicit sort
//    (`sort_field=None`). Verified live: `?sort=id:desc` on chunks → HTTP 400.
// 2. Each request re-resolves the ACTIVE build, so a build activated mid-pagination
//    would splice page 1 (old build) with page 2 (new) — a silently mixed corpus.
//    Every page carries `meta.build_id`; the page pins it and fails loud on a swap.
//    (No active build at all is a 409 NO_ACTIVE_BUILD, verified live — never a 200
//    with an empty list, so an empty table really does mean an empty build.)
// 3. A missing row answers 404 with `error.code = "VALIDATION_ERROR"` — `code_for_status`
//    maps EVERY 4xx to that code, so it cannot distinguish "gone" from "bad request".
//    Branch on the HTTP STATUS.

const INSPECT_PAGE = 50;

/** One page of a build-scoped list: its rows, the build that served them, and the
 *  cursor to the next page (absent = last page). ``total`` is the exact match
 *  count when the endpoint reports it (SS1b: entities/documents with server-side
 *  ``q``), absent otherwise — so an honest "N matches" can replace a loaded-rows
 *  count. */
export type InspectPage<T> = { rows: T[]; buildId: string | null; next?: string; total?: number };

/** An API failure that keeps the frozen error CODE. The list pages are only valid while
 *  the build that served them is still active, and the code is what says whether it is. */
type ErrorCode = components["schemas"]["ErrorCode"];

class ApiError extends Error {
  readonly code: ErrorCode | "";

  constructor(code: ErrorCode | "", message: string) {
    super(message);
    this.code = code;
  }
}

/** Codes that say NOTHING about the build that served the pages already on screen: the store
 *  was down, we were throttled, the server faulted, it timed out. The corpus is untouched, so
 *  a load-more that hits one of these may keep the loaded rows.
 *
 *  This is an ALLOWLIST, and the direction is the point. Both of `inspect.py::_bind`'s failure
 *  exits prove the scope that served those rows is gone — NO_ACTIVE_BUILD (409, the build was
 *  deactivated) and PROJECT_NOT_FOUND (404: `delete_project` refuses while any build exists, so
 *  the project being gone means its builds are too). Listing the REJECTS instead would close
 *  today's two spellings and leave the branch open for the next: §27.2's ErrorCode vocabulary is
 *  additive-only, and a code added later must not silently inherit the branch that keeps a
 *  possibly-vanished corpus on screen. Fail closed by default; earn the rows back explicitly. */
const SCOPE_NEUTRAL = new Set<ErrorCode>([
  "STORE_UNAVAILABLE",
  "RATE_LIMITED",
  "INTERNAL",
  "QUERY_TIMEOUT",
]);

/** Builds the thrown error from the API's error envelope. The `??`s are not defensive noise:
 *  a body that is NOT our envelope (a proxy's HTML 502, say) has no `error.code` to read, and
 *  the empty code deliberately falls OUTSIDE the allowlist below — an unparseable failure tells
 *  us nothing about the binding, and "nothing" fails closed here. Reading it unguarded would
 *  instead throw a TypeError and show the user "Cannot read properties of undefined". */
function apiError(body: { error?: { code?: ErrorCode; message?: string } }): ApiError {
  return new ApiError(body.error?.code ?? "", body.error?.message ?? "the request failed");
}

/** True when the failure CANNOT have invalidated the build that served the loaded pages —
 *  either the server never answered at all (a transport error, so it said nothing about the
 *  binding) or it answered with a scope-neutral code above.
 *
 *  Note this keys on the CODE while the detail read below keys on the STATUS. The asymmetry is
 *  real: these codes are raised deliberately by the API, whereas a detail 404's code is the
 *  COARSE fallback `code_for_status` stamps on every framework-raised 4xx (VALIDATION_ERROR),
 *  which identifies nothing. And status alone would be a spelling here — 409 is shared by
 *  BUILD_NOT_READY and JOB_CONFLICT; the code names the condition itself. */
export function isScopeNeutral(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true; // the server never answered — it said nothing
  return error.code !== "" && SCOPE_NEUTRAL.has(error.code); // unparseable body ⇒ fail closed
}

function useInspectList<T>(
  key: string,
  project: string | undefined,
  fetchPage: (project: string, cursor?: string, q?: string) => Promise<InspectPage<T>>,
  q?: string,
) {
  return useInfiniteQuery({
    // q is part of the key: a new search is a new query (its own cache entry +
    // cursor chain), so changing q re-fetches from page 1, and load-more keeps
    // the same q. Lists without server-side search pass q=undefined (unchanged
    // key shape). Callers that don't search leave q undefined.
    queryKey: [key, project, q],
    enabled: project !== undefined && isPathAddressable(project),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => fetchPage(project as string, pageParam, q),
    getNextPageParam: (last: InspectPage<T>) => last.next,
  });
}

async function fetchDocuments(project: string, cursor?: string): Promise<InspectPage<Document>> {
  const { data, error } = await api.GET("/projects/{project}/documents", {
    params: { path: { project }, query: { limit: INSPECT_PAGE, cursor } },
  });
  if (error) throw apiError(error);
  return { rows: data.data, buildId: data.meta.build_id, next: data.meta.next_cursor ?? undefined };
}

async function fetchChunks(project: string, cursor?: string): Promise<InspectPage<Chunk>> {
  const { data, error } = await api.GET("/projects/{project}/chunks", {
    params: { path: { project }, query: { limit: INSPECT_PAGE, cursor } },
  });
  if (error) throw apiError(error);
  return { rows: data.data, buildId: data.meta.build_id, next: data.meta.next_cursor ?? undefined };
}

export const useDocuments = (project: string | undefined) =>
  useInspectList("documents", project, fetchDocuments);
export const useChunks = (project: string | undefined) =>
  useInspectList("chunks", project, fetchChunks);

// A 404 here means "no such row IN THE ACTIVE BUILD" — ids are minted per build, so a
// row id from a superseded build cannot resolve in the current one. Say that, rather
// than echoing the generic VALIDATION_ERROR message every 4xx carries.
/** A detail read's 404 PROVES the world moved: the inspect detail endpoints
 *  return rows regardless of lifecycle status, so the only 404 is
 *  id-absent-from-build — a build swap (or a deleted project). Typed so pages
 *  that must fail closed page-wide on scope loss (Graph) can classify it;
 *  Inspect renders the message locally, which is also honest for its layout. */
export class DetailScopeGoneError extends Error {}

function detailError(status: number, message: string, code?: string): Error {
  if (status === 404)
    return new DetailScopeGoneError(
      "Not found in the active build — it may belong to an older build, or the active build changed. Reload the list.",
    );
  // the deliberate scope codes are the same proof at a different status (the
  // round-6 code-vs-status lesson applied to the detail path too): the server's
  // own message survives, only the CLASS is sharpened — behavior-neutral for
  // Inspect, which renders message(error) with no type branch
  if (code === "NO_ACTIVE_BUILD" || code === "PROJECT_NOT_FOUND")
    return new DetailScopeGoneError(message);
  return new Error(message);
}

// Detail reads. `Document.raw` is returned ONLY here — the list omits the key entirely
// (verified against a real build), which is what a row click is for.
export function useDocument(project: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["document", project, id],
    enabled: project !== undefined && isPathAddressable(project) && id !== undefined,
    // same rule as useRelation: a detail 404 is deterministic, don't retry it
    retry: (failureCount, error) => !(error instanceof DetailScopeGoneError) && failureCount < 3,
    queryFn: async () => {
      const { data, error, response } = await api.GET(
        "/projects/{project}/documents/{document_id}",
        { params: { path: { project: project as string, document_id: id as string } } },
      );
      if (error) throw detailError(response.status, error.error.message, error.error.code);
      return data.data;
    },
  });
}

export function useChunk(project: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["chunk", project, id],
    enabled: project !== undefined && isPathAddressable(project) && id !== undefined,
    // same rule as useRelation: a detail 404 is deterministic, don't retry it
    retry: (failureCount, error) => !(error instanceof DetailScopeGoneError) && failureCount < 3,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/projects/{project}/chunks/{chunk_id}", {
        params: { path: { project: project as string, chunk_id: id as string } },
      });
      if (error) throw detailError(response.status, error.error.message, error.error.code);
      return data.data;
    },
  });
}

// ---- FE2 清洗 (DESIGN §10.2, contract v1.1 DR-009) --------------------------------
//
// Two facts drive the shapes here (read from api/routers/projects.py and
// core/registry/store.py, not assumed):
// 1. PATCH /projects/{project} REPLACES the whole config column — there is no
//    server-side deep merge. Writing chunking params must therefore spread the
//    project's CURRENT config and override only the chunking block; PATCHing
//    {config: {chunking}} alone would silently WIPE ontology and every other
//    block, and the wreck would surface only at the next build.
// 2. The preview is a pure RPC (nothing persisted, no Idempotency-Key), so it
//    is a mutation, not a query: no cache entry means no stale-while-revalidate
//    window for class 17 to bite — the page owns "these chunks answer THESE
//    parameters" staleness explicitly instead.

export type CleanPreviewRequest = components["schemas"]["CleanPreviewRequest"];
export type CleanPreviewChunk = components["schemas"]["CleanPreviewChunk"];
export type CleanPreviewResult = { chunks: CleanPreviewChunk[]; buildId: string | null };

/** Engine chunking defaults — DISPLAY mirror of core/clean/chunking.py's
 *  DEFAULT_MAX_CHARS/DEFAULT_OVERLAP. Only placeholders and the save button's
 *  label read these; every REAL fallback happens server-side (the preview and
 *  the build walk the same config→default chain), so a drift here mislabels a
 *  placeholder but cannot change what runs. */
export const DEFAULT_CHUNKING = { max_chars: 1200, overlap: 200 } as const;

/** The project's configured chunking values with engine-default fallback —
 *  same per-field, bool-excluded read the preview endpoint does server-side
 *  (bool is an int subtype in Python AND accepted by JS typeof checks nowhere,
 *  but a config written by hand can hold anything). */
export function chunkingFromConfig(config: Record<string, unknown>): {
  max_chars: number;
  overlap: number;
} {
  const block = config["chunking"];
  const pick = (key: string, fallback: number): number => {
    if (block && typeof block === "object" && !Array.isArray(block)) {
      const v = (block as Record<string, unknown>)[key];
      if (typeof v === "number" && Number.isInteger(v)) return v;
    }
    return fallback;
  };
  return {
    max_chars: pick("max_chars", DEFAULT_CHUNKING.max_chars),
    overlap: pick("overlap", DEFAULT_CHUNKING.overlap),
  };
}

/** Mirror of the SERVER's verdict on whether a PRESENT chunking block would
 *  load (core/builds/config.py _load_chunking): object-only, keys ⊆
 *  {max_chars, overlap}, present leaves are integers (bool/float/string
 *  rejected). The pair relation (0 ≤ overlap < max_chars) is deliberately NOT
 *  checked here — the build defers it to the clean stage (which the form's
 *  pairError already surfaces), so a bad pair is config-load VALID and needs
 *  an operator edit, not a one-click repair. A config-load-malformed block
 *  (a typo'd key, null, a non-integer leaf) whose salvage is a clean pair is
 *  what this flags for the no-edit repair (Codex #79 R8). Pinned to the real
 *  loader by tests/fixtures/chunking_block_validity.json. */
export function isValidChunkingBlock(block: unknown): boolean {
  if (!isRecord(block)) return false; // _mapping raises on a non-object
  for (const k of Object.keys(block)) if (k !== "max_chars" && k !== "overlap") return false;
  for (const k of ["max_chars", "overlap"]) {
    if (k in block) {
      const v = block[k];
      // _int (bool is typeof "boolean"; a non-whole float fails Number.isInteger)
      // IRREDUCIBLE GAP: a WHOLE-number JSON float (500.0) parses to 500 here —
      // JS has no int/float distinction post-parse — so this mirror accepts it,
      // while the server's _int rejects the Python float. Unreachable from the
      // Settings/Clean writers (they emit real integers) and only a hand-written
      // `500.0` trips it. PATCH does NOT validate config today (that is the whole
      // silent-brick premise), so the only place that COULD close this residue is
      // a future server-side PATCH validator; no such guard exists now. Left
      // uncovered on purpose; see the corpus.
      if (typeof v !== "number" || !Number.isInteger(v)) return false;
    }
  }
  return true;
}

/** True when a PRESENT chunking block would be rejected at build config load
 *  — the form treats it as repairable (Codex #79 R8, the chunking sibling of
 *  the ontology R4). An absent block is the legal default state, not malformed. */
export function chunkingMalformed(config: Record<string, unknown>): boolean {
  return (
    Object.prototype.hasOwnProperty.call(config, "chunking") &&
    !isValidChunkingBlock(config["chunking"])
  );
}

/** The single project — FE2 needs the full config object to spread on save. */
export function useProject(project: string | undefined) {
  return useQuery({
    queryKey: ["project", project],
    enabled: project !== undefined && isPathAddressable(project),
    queryFn: async () => {
      const { data, error } = await api.GET("/projects/{project}", {
        params: { path: { project: project as string } },
      });
      if (error) throw new Error(error.error.message);
      return data.data;
    },
  });
}

/** Preview chunking (POST clean/preview). meta.build_id names the active build
 *  the document was read from; null for the text source. */
export function usePreviewClean(project: string) {
  return useMutation({
    mutationFn: async (body: CleanPreviewRequest): Promise<CleanPreviewResult> => {
      const { data, error } = await api.POST("/projects/{project}/clean/preview", {
        params: { path: { project } },
        body,
      });
      if (error) throw new Error(error.error.message);
      return { chunks: data.data.chunks, buildId: data.meta.build_id };
    },
  });
}

/** Write the chunking block into project config — spreading a FRESH read
 *  (fact 1 above). The mutation re-GETs the project INSIDE itself rather than
 *  spreading the page's cached copy: a config changed elsewhere since the page
 *  loaded (another tab, a CLI PATCH) would otherwise be resurrected wholesale
 *  by the column-replacing PATCH (Codex, #74). The refetch shrinks that window
 *  from page-age to one round trip; the residual concurrent-write race is real
 *  (a recheck is not a lock — class 10) but closing it needs a version token
 *  in the frozen contract, a DR-002 round this task cannot open. */
/** The shared mutationKey for the three Settings section saves. The page reads
 *  its in-flight count via useIsMutating to lock EVERY section while ANY save is
 *  pending — because each save spreads its own FRESH read of the whole config
 *  column, two same-page saves launched before the first PATCH lands both read
 *  the pre-save config and the later PATCH drops the earlier section's change
 *  (Codex #79 R10, a lost update). This closes the SAME-PAGE double-submit
 *  fully; the CROSS-WRITER race (another tab / the CLI) is the separate,
 *  version-token-shaped gap the save docstrings note. project-scoped so the key
 *  never matches another project's saves. */
export const settingsSaveMutationKey = (project: string) => ["settings-save", project] as const;

export function useSaveChunking(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: settingsSaveMutationKey(project),
    mutationFn: async (args: { max_chars?: number; overlap?: number }) => {
      const fresh = await api.GET("/projects/{project}", {
        params: { path: { project } },
      });
      if (fresh.error) throw new Error(fresh.error.error.message);
      const current = (fresh.data.data.config ?? {}) as Record<string, unknown>;
      // Omissions resolve against the FRESH chunking block, not the page's cached
      // one (Codex, #74 round 2): resolving before the re-read preserved sibling
      // blocks but silently reverted a knob someone else had just changed. And the
      // combined pair must re-validate HERE — fresh fallbacks can make a typed knob
      // illegal, and PATCH does not validate chunking (it would fail at the next
      // build's config load instead).
      const freshPair = chunkingFromConfig(current);
      const pair = {
        max_chars: args.max_chars ?? freshPair.max_chars,
        overlap: args.overlap ?? freshPair.overlap,
      };
      if (!(0 <= pair.overlap && pair.overlap < pair.max_chars))
        throw new Error(
          `overlap must satisfy 0 <= overlap < max_chars (got ${pair.overlap} / ${pair.max_chars} ` +
            "after resolving against the project's current config — it changed since this page loaded)",
        );
      const { data, error } = await api.PATCH("/projects/{project}", {
        params: { path: { project } },
        body: { config: { ...current, chunking: pair } },
      });
      if (error) throw new Error(error.error.message);
      return { project: data.data, pair };
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["project", project] }),
  });
}

// ---- FE4 圖譜互動探索 (DESIGN §10.2) ------------------------------------------
//
// Facts read from api/routers/inspect.py (not assumed):
// * The subgraph endpoint REQUIRES a query_policy block in project config —
//   missing → 400 with details.query_policy="missing" (a condition the page
//   must name, not blur into a generic failure); hops beyond max_graph_hops
//   are REJECTED, not clamped.
// * Entity lists NOW support server-side search (SS1b): GET /entities takes a
//   `q` substring over canonical_name and reports an exact `total`, so the left
//   column is a REAL search over the whole active build (not the FE3 client-side
//   over-loaded-pages filter) — the affordance is honest, no caveat needed.
// * Relation.evidence[] rides ONLY the detail GET — clicking an edge is a
//   real fetch, exactly like Document.raw in FE3.

export type Entity = components["schemas"]["Entity"];
export type Relation = components["schemas"]["Relation"];
export type GraphContext = components["schemas"]["GraphContext"];

async function fetchEntities(
  project: string,
  cursor?: string,
  q?: string,
): Promise<InspectPage<Entity>> {
  const { data, error } = await api.GET("/projects/{project}/entities", {
    // SS1b: q is server-side substring search over canonical_name. Send it only
    // when non-empty — an empty string would be a distinct (no-op) search key.
    params: { path: { project }, query: { limit: INSPECT_PAGE, cursor, q: q || undefined } },
  });
  if (error) throw apiError(error);
  return {
    rows: data.data,
    buildId: data.meta.build_id,
    next: data.meta.next_cursor ?? undefined,
    total: data.meta.total ?? undefined,
  };
}

export const useEntities = (project: string | undefined, q?: string) =>
  useInspectList("entities", project, fetchEntities, q);

export function useEntity(project: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["entity", project, id],
    enabled: project !== undefined && isPathAddressable(project) && id !== undefined,
    // same rule as useRelation: a detail 404 is deterministic, don't retry it
    retry: (failureCount, error) => !(error instanceof DetailScopeGoneError) && failureCount < 3,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/projects/{project}/entities/{entity_id}", {
        params: { path: { project: project as string, entity_id: id as string } },
      });
      if (error) throw detailError(response.status, error.error.message, error.error.code);
      return data.data;
    },
  });
}

export function useRelation(project: string | undefined, id: string | undefined) {
  return useQuery({
    queryKey: ["relation", project, id],
    enabled: project !== undefined && isPathAddressable(project) && id !== undefined,
    // DetailScopeGoneError is deterministic (id absent from the active build)
    // — retrying delays the scope verdict (Codex #76 R6)
    retry: (failureCount, error) => !(error instanceof DetailScopeGoneError) && failureCount < 3,
    queryFn: async () => {
      const { data, error, response } = await api.GET(
        "/projects/{project}/relations/{relation_id}",
        { params: { path: { project: project as string, relation_id: id as string } } },
      );
      if (error) throw detailError(response.status, error.error.message, error.error.code);
      return data.data;
    },
  });
}

/** The distinguished "this project has no query_policy configured" condition —
 *  the subgraph endpoint 400s it with a machine-readable detail, and the page
 *  offers configuration guidance instead of a generic failure line. */
export class PolicyMissingError extends Error {}

/** A subgraph failure that PROVES the page's world moved: the active build is
 *  gone/changed (NO_ACTIVE_BUILD, PROJECT_NOT_FOUND) or the seed no longer
 *  resolves in the current build (404 — either a build swap or the entity's
 *  status drifted off active; both mean the listed rows are stale). Classified
 *  AT THROW TIME because the code alone is ambiguous: a seed miss carries the
 *  coarse VALIDATION_ERROR that a hops rejection (a plain user-input 400 that
 *  must stay LOCAL) also carries — the STATUS is what separates them (the FE3
 *  lesson). */
export class SubgraphScopeError extends Error {}

export type SubgraphResult = { graph: GraphContext; buildId: string | null };

export function useSubgraph(
  project: string | undefined,
  entityId: string | undefined,
  hops: number,
) {
  return useQuery({
    queryKey: ["subgraph", project, entityId, hops],
    enabled: project !== undefined && isPathAddressable(project) && entityId !== undefined,
    // Scope-proof and policy-missing errors are deterministic contract states,
    // not transient faults — retrying only delays the verdict consumers must
    // act on (the review queue uses it as a WRITE lock: Codex #76 R6). Neutral
    // failures keep the default three attempts.
    retry: (failureCount, error) =>
      !(error instanceof SubgraphScopeError || error instanceof PolicyMissingError) &&
      failureCount < 3,
    queryFn: async (): Promise<SubgraphResult> => {
      const { data, error, response } = await api.GET("/projects/{project}/graph/subgraph", {
        params: {
          path: { project: project as string },
          query: { entity_id: entityId as string, hops },
        },
      });
      if (error) {
        const details = (error.error as { details?: { query_policy?: string } }).details;
        if (details?.query_policy === "missing") throw new PolicyMissingError(error.error.message);
        const code = error.error.code;
        if (code === "NO_ACTIVE_BUILD" || code === "PROJECT_NOT_FOUND" || response.status === 404)
          throw new SubgraphScopeError(error.error.message);
        throw new Error(error.error.message);
      }
      return { graph: data.data, buildId: data.meta.build_id };
    },
  });
}

// ---- UXB1 設定頁 (DESIGN §6/§21) ---------------------------------------------
//
// Facts read from api/routers/projects.py, core/builds/config.py,
// api/routers/query.py and contracts/query_policy.schema.json (not assumed):
// * PATCH validates NOTHING about config block content. Each block fails at
//   its own later moment — ontology at the next BUILD's config load,
//   query_policy at the next QUERY (400, details.query_policy missing|invalid).
//   The client-side mirrors below are the only pre-flight guard an operator
//   gets; they never replace the server verdict (a PATCH/build/query error
//   still surfaces verbatim when a mirror misses).
// * ontology semantics (core/builds/config.py _load_ontology + TextOntology):
//   the OMITTED key is the only legal "no vocabulary" — an explicit null is
//   malformed, and a PRESENT block requires BOTH type lists non-empty. Saving
//   an empty vocabulary therefore DELETES the key rather than writing {} or
//   null. proposal_policy ∈ ("review", "auto") — core/graph/proposals.py.
// * Every save spreads a FRESH config read inside the mutation (the
//   useSaveChunking discipline, Codex #74): PATCH replaces the whole column,
//   so a stale spread would resurrect config someone else already changed.

export type OntologyDraft = {
  entityTypes: string[];
  relationTypes: string[];
  proposalPolicy: "review" | "auto";
};

/** The two legal ontology proposal policies — mirror of PROPOSAL_POLICIES in
 *  core/graph/proposals.py. Drift is caught by the ontology parity corpus. */
export const PROPOSAL_POLICIES = ["review", "auto"] as const;

/** Mirror of the SERVER's verdict on whether a PRESENT ontology block would
 *  load (core/builds/config.py _load_ontology + TextOntology): the block must
 *  be an object, carry only {entity_types, relation_types, proposal_policy},
 *  a present proposal_policy must be one of PROPOSAL_POLICIES, the two type
 *  fields (when present) must be string arrays, and BOTH must resolve
 *  non-empty. A block that fails this raises at the next BUILD while the
 *  settings PATCH "succeeds" — the same silent brick the policy mirror guards
 *  (Codex #79 R4, the ontology sibling). Pinned to the real loader by
 *  tests/fixtures/ontology_block_validity.json (the query_policy parity
 *  pattern); the absent key is NOT this function's concern (it is the legal
 *  no-vocabulary state, handled by the caller). */
export function isValidOntologyBlock(block: unknown): boolean {
  if (!isRecord(block)) return false; // _mapping raises on a non-object
  for (const k of Object.keys(block))
    if (k !== "entity_types" && k !== "relation_types" && k !== "proposal_policy") return false;
  if ("proposal_policy" in block) {
    const p = block["proposal_policy"];
    if (
      typeof p !== "string" ||
      !PROPOSAL_POLICIES.includes(p as (typeof PROPOSAL_POLICIES)[number])
    )
      return false;
  }
  // TextOntology rejects blank/whitespace-only type values too (ontology.py:
  // `if not value.strip()`), not just an empty list — a bare _str_list check
  // would accept [""] which the build refuses
  const strList = (v: unknown): v is string[] =>
    Array.isArray(v) && v.every((s) => typeof s === "string" && s.trim().length > 0);
  const ents = "entity_types" in block ? block["entity_types"] : [];
  const rels = "relation_types" in block ? block["relation_types"] : [];
  if (!strList(ents) || !strList(rels)) return false;
  return ents.length > 0 && rels.length > 0; // TextOntology requires both
}

/** The project's ontology block as the settings form sees it — defensive
 *  reads, same spirit as chunkingFromConfig (hand-written config can hold
 *  anything). `present` distinguishes "no vocabulary declared" from an
 *  empty-read block; `malformed` flags a PRESENT block the build would reject
 *  (Codex #79 R4) so the page treats it as repairable rather than clean — the
 *  salvaged (filtered/fallback) values shown below are what a repair save
 *  writes. */
export function ontologyFromConfig(
  config: Record<string, unknown>,
): OntologyDraft & { present: boolean; malformed: boolean } {
  const keyExists = Object.prototype.hasOwnProperty.call(config, "ontology");
  const block = config["ontology"];
  const malformed = keyExists && !isValidOntologyBlock(block);
  // salvage EXACTLY what a save would write (normalizeTypes: trim + drop
  // blanks + dedup) so the "整理後的版本" the malformed notice promises is the
  // block the repair save actually persists — a raw pass-through would show a
  // blank chip the mutation then strips, desyncing the form from the write
  const strings = (v: unknown): string[] =>
    normalizeTypes(Array.isArray(v) ? v.filter((s): s is string => typeof s === "string") : []);
  if (isRecord(block)) {
    return {
      present: true,
      malformed,
      entityTypes: strings(block["entity_types"]),
      relationTypes: strings(block["relation_types"]),
      proposalPolicy: block["proposal_policy"] === "auto" ? "auto" : "review",
    };
  }
  return {
    present: false,
    malformed,
    entityTypes: [],
    relationTypes: [],
    proposalPolicy: "review",
  };
}

function normalizeTypes(values: string[]): string[] {
  const out: string[] = [];
  for (const raw of values) {
    const v = raw.trim();
    if (v !== "" && !out.includes(v)) out.push(v);
  }
  return out;
}

/** The ontology fields the operator actually EDITED (undefined = untouched). */
export type OntologyEdits = {
  entityTypes?: string[];
  relationTypes?: string[];
  proposalPolicy?: "review" | "auto";
};

/** Write (or delete) the ontology block. Untouched fields resolve from the
 *  FRESH block, never the page's snapshot (the useSaveChunking discipline,
 *  Codex #74/#79 R7): editing one field must not silently revert a concurrent
 *  change to another. ontologyFromConfig(current) yields the fresh view for
 *  every case — a valid block's vocabulary, a malformed block's salvage, or an
 *  absent block's empty view (so a concurrent delete is respected, not
 *  resurrected). Empty resolved vocabulary = DELETE the key; a one-sided
 *  result is refused HERE because the build would refuse it LATER
 *  (TextOntology requires both lists). */
export function useSaveOntology(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: settingsSaveMutationKey(project),
    mutationFn: async (edits: OntologyEdits) => {
      const fresh = await api.GET("/projects/{project}", {
        params: { path: { project } },
      });
      if (fresh.error) throw new Error(fresh.error.error.message);
      const current = (fresh.data.data.config ?? {}) as Record<string, unknown>;
      const freshView = ontologyFromConfig(current);
      const entityTypes = normalizeTypes(edits.entityTypes ?? freshView.entityTypes);
      const relationTypes = normalizeTypes(edits.relationTypes ?? freshView.relationTypes);
      const proposalPolicy = edits.proposalPolicy ?? freshView.proposalPolicy;
      if ((entityTypes.length === 0) !== (relationTypes.length === 0))
        throw new Error(
          "實體類型與關係類型必須同時提供,或同時清空(移除整份詞彙表)——只填一邊的詞彙表會在下次建置時失敗",
        );
      const { ontology: _dropped, ...rest } = current;
      const nextConfig =
        entityTypes.length === 0
          ? rest
          : {
              ...rest,
              ontology: {
                entity_types: entityTypes,
                relation_types: relationTypes,
                proposal_policy: proposalPolicy,
              },
            };
      const { data, error } = await api.PATCH("/projects/{project}", {
        params: { path: { project } },
        body: { config: nextConfig },
      });
      if (error) throw new Error(error.error.message);
      return {
        project: data.data,
        saved: { entityTypes, relationTypes, proposalPolicy },
      };
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["project", project] }),
  });
}

/** A COMPLETE, schema-valid query_policy for projects that have none yet —
 *  every top-level field is required by the frozen contract, so the form
 *  cannot write just the three operator knobs into the void. Safety posture:
 *  sql/cypher disabled, block lists at the schema's frozen minimums.
 *  Pinned against contracts/query_policy.schema.json by
 *  queryPolicyTemplate.test.ts (required keys, consts, frozen contains). */
export const DEFAULT_QUERY_POLICY = {
  schema_version: "1.0",
  default_mode: "hybrid",
  max_top_k: 10,
  max_graph_hops: 2,
  max_sql_rows: 100,
  max_latency_ms: 15000,
  require_sources: true,
  expose_debug: false,
  text_to_sql: {
    enabled: false,
    readonly: true,
    allowed_tables: [],
    blocked_keywords: ["insert", "update", "delete", "drop", "alter", "truncate"],
    max_rows: 100,
    timeout_ms: 10000,
  },
  text_to_cypher: {
    enabled: false,
    readonly: true,
    allowed_clauses: ["MATCH", "WHERE", "RETURN", "LIMIT"],
    blocked: ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "CALL"],
    max_rows: 100,
    timeout_ms: 10000,
  },
} as const;

export type QueryPolicyOperator = {
  defaultMode: QueryMode;
  maxTopK: number;
  maxGraphHops: number;
};

/** The policy's operator-facing fields (and the flags the form needs) with
 *  template fallback — defensive reads for the same reason as above. */
function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

/** Mirror of the SERVER's policy validity verdict — the predicate deciding
 *  whether an existing query_policy is a usable spread base or must be
 *  rebuilt on the template. The server's oracle is query_policy_from_mapping
 *  (core/mcp/policy.py): frozen-schema jsonschema validation + the §21 typed
 *  re-checks; a hand-written block that fails it 400s EVERY query while the
 *  settings PATCH "succeeds" (Codex #79 R2 for missing keys, R3 for
 *  key-complete blocks violating VALUE constraints — bad schema_version, an
 *  enabled sql with an empty whitelist, a shrunken frozen blocked list).
 *  Parity is enforced mechanically from one corpus both suites read
 *  (tests/fixtures/query_policy_validity.json; the pytest half runs the REAL
 *  validator — the fileUriGate pattern), so this mirror cannot drift
 *  silently. Frozen lists derive from DEFAULT_QUERY_POLICY, which
 *  queryPolicyTemplate.test.ts pins to the schema's contains/enum clauses. */
export function isValidPolicyBlock(block: unknown): boolean {
  if (!isRecord(block)) return false;
  const T = DEFAULT_QUERY_POLICY;
  const sameKeys = (a: Record<string, unknown>, b: Record<string, unknown>): boolean => {
    const ak = Object.keys(a).sort();
    const bk = Object.keys(b).sort();
    return ak.length === bk.length && ak.every((k, i) => k === bk[i]);
  };
  const posInt = (v: unknown): boolean => typeof v === "number" && Number.isInteger(v) && v >= 1;
  const strList = (v: unknown, pattern?: RegExp): v is string[] =>
    Array.isArray(v) &&
    v.every((s) => typeof s === "string" && s.length > 0 && (!pattern || pattern.test(s))) &&
    new Set(v).size === v.length;

  // top level: exact key set (all required, additionalProperties false)
  if (!sameKeys(block, T)) return false;
  if (block["schema_version"] !== "1.0") return false;
  const modes: readonly QueryMode[] = ["semantic", "graph", "sql", "global", "hybrid"];
  if (!modes.includes(block["default_mode"] as QueryMode)) return false;
  for (const k of ["max_top_k", "max_graph_hops", "max_sql_rows", "max_latency_ms"])
    if (!posInt(block[k])) return false;
  if (block["require_sources"] !== true) return false;
  if (typeof block["expose_debug"] !== "boolean") return false;

  const sql = block["text_to_sql"];
  if (!isRecord(sql) || !sameKeys(sql, T.text_to_sql)) return false;
  if (typeof sql["enabled"] !== "boolean" || sql["readonly"] !== true) return false;
  if (!strList(sql["allowed_tables"])) return false;
  if (!strList(sql["blocked_keywords"], /^[a-z_]+$/)) return false;
  for (const kw of T.text_to_sql.blocked_keywords)
    if (!(sql["blocked_keywords"] as string[]).includes(kw)) return false;
  if (!posInt(sql["max_rows"]) || !posInt(sql["timeout_ms"])) return false;
  // enabled sql with an empty whitelist is a deny-all contradiction (if/then)
  if (sql["enabled"] === true && (sql["allowed_tables"] as string[]).length === 0) return false;

  const cy = block["text_to_cypher"];
  if (!isRecord(cy) || !sameKeys(cy, T.text_to_cypher)) return false;
  if (typeof cy["enabled"] !== "boolean" || cy["readonly"] !== true) return false;
  if (!strList(cy["allowed_clauses"])) return false;
  const clauses = cy["allowed_clauses"] as string[];
  if (clauses.length < 1) return false;
  const universe: readonly string[] = T.text_to_cypher.allowed_clauses;
  if (!clauses.every((c) => universe.includes(c))) return false;
  if (!strList(cy["blocked"], /^[A-Z_]+$/)) return false;
  for (const kw of T.text_to_cypher.blocked)
    if (!(cy["blocked"] as string[]).includes(kw)) return false;
  if (!posInt(cy["max_rows"]) || !posInt(cy["timeout_ms"])) return false;

  // the schema's allOf: a default of sql needs sql enabled
  if (block["default_mode"] === "sql" && sql["enabled"] !== true) return false;
  return true;
}

export function policyFromConfig(config: Record<string, unknown>): QueryPolicyOperator & {
  present: boolean;
  malformed: boolean;
  sqlEnabled: boolean;
  cypherEnabled: boolean;
  sqlBlock: unknown;
  cypherBlock: unknown;
} {
  const block = config["query_policy"];
  const b = isRecord(block) ? block : undefined;
  // an incomplete block will be REBUILT on the template at save time, so the
  // form's derived flags (sql option, 進階 folds) must describe the template,
  // not the junk — only the three operator fields are salvaged for seeding
  const malformed = b !== undefined && !isValidPolicyBlock(b);
  const usable = b !== undefined && !malformed ? b : undefined;
  // the operator knobs seed the form, so an OUT-OF-RANGE salvaged value
  // (max_top_k: 0 in a malformed block) would trip the form's own fieldError
  // and disable the very rebuild button meant to repair it (Codex #79 R5,
  // the R3 value-domain class in the salvage) — fall back to the template
  // default, same as the sql default_mode guard below. Schema minimum is 1.
  const int = (v: unknown, fallback: number): number =>
    typeof v === "number" && Number.isInteger(v) && v >= 1 ? v : fallback;
  const enabled = (sub: unknown): boolean => isRecord(sub) && sub["enabled"] === true;
  const modes: readonly QueryMode[] = ["semantic", "graph", "sql", "global", "hybrid"];
  const mode = b?.["default_mode"];
  return {
    present: b !== undefined,
    malformed,
    // a malformed block rebuilds on the template, whose text_to_sql is
    // disabled — salvaging a junk default_mode of "sql" would seed the form
    // to a value its own save refuses (the R1 dead-end, one click later)
    defaultMode:
      modes.includes(mode as QueryMode) && !(malformed && mode === "sql")
        ? (mode as QueryMode)
        : DEFAULT_QUERY_POLICY.default_mode,
    maxTopK: int(b?.["max_top_k"], DEFAULT_QUERY_POLICY.max_top_k),
    maxGraphHops: int(b?.["max_graph_hops"], DEFAULT_QUERY_POLICY.max_graph_hops),
    sqlEnabled: usable !== undefined ? enabled(usable["text_to_sql"]) : false,
    cypherEnabled: usable !== undefined ? enabled(usable["text_to_cypher"]) : false,
    sqlBlock: usable?.["text_to_sql"] ?? DEFAULT_QUERY_POLICY.text_to_sql,
    cypherBlock: usable?.["text_to_cypher"] ?? DEFAULT_QUERY_POLICY.text_to_cypher,
  };
}

/** What a policy save carries: the operator fields the user actually EDITED
 *  (undefined = untouched), plus the page's SALVAGED view of all three as the
 *  fallback for a rebuild (where the fresh block is invalid and offers no
 *  value to resolve an untouched field against). */
export type QueryPolicySave = {
  edits: { defaultMode?: QueryMode; maxTopK?: number; maxGraphHops?: number };
  salvaged: QueryPolicyOperator;
};

/** Write the operator fields into query_policy — spreading the FRESH block
 *  (or the full template when the project has none / is malformed: the frozen
 *  schema requires every field, so a partial write would brick every query).
 *  Untouched operator fields resolve from the FRESH block, not the page's
 *  snapshot (the useSaveChunking discipline, Codex #74/#79 R6): saving one
 *  edited knob must not silently revert a concurrent change to another. When
 *  the fresh block is invalid there is no value to preserve, so a rebuild
 *  falls back to the page's salvaged view. The sql-mode cross-check mirrors
 *  the schema's default_mode/text_to_sql allOf against the FRESH base — a
 *  recheck, not a lock (class 10). */
export function useSaveQueryPolicy(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: settingsSaveMutationKey(project),
    mutationFn: async ({ edits, salvaged }: QueryPolicySave) => {
      const posInt = (v: number, label: string) => {
        if (!Number.isInteger(v) || v < 1) throw new Error(`${label}必須是 ≥ 1 的整數`);
      };
      if (edits.maxTopK !== undefined) posInt(edits.maxTopK, "單次檢索筆數上限(max_top_k)");
      if (edits.maxGraphHops !== undefined)
        posInt(edits.maxGraphHops, "圖譜跳數上限(max_graph_hops)");
      const fresh = await api.GET("/projects/{project}", {
        params: { path: { project } },
      });
      if (fresh.error) throw new Error(fresh.error.error.message);
      const current = (fresh.data.data.config ?? {}) as Record<string, unknown>;
      const block = current["query_policy"];
      // a PARTIAL/malformed block (the curl-only era) must not be the spread
      // base: the PATCH would "succeed" while queries keep 400ing on the
      // missing required fields (Codex #79 R2) — rebuild it on the template.
      const usable = isValidPolicyBlock(block);
      const base: Record<string, unknown> = usable
        ? (block as Record<string, unknown>)
        : { ...DEFAULT_QUERY_POLICY };
      // untouched fields resolve from the FRESH valid block (preserve a
      // concurrent edit); a rebuild has no valid fresh source, so it falls
      // back to the salvaged view the operator was looking at (R6).
      const freshOps: QueryPolicyOperator = usable
        ? {
            defaultMode: base["default_mode"] as QueryMode,
            maxTopK: base["max_top_k"] as number,
            maxGraphHops: base["max_graph_hops"] as number,
          }
        : salvaged;
      const resolved: QueryPolicyOperator = {
        defaultMode: edits.defaultMode ?? freshOps.defaultMode,
        maxTopK: edits.maxTopK ?? freshOps.maxTopK,
        maxGraphHops: edits.maxGraphHops ?? freshOps.maxGraphHops,
      };
      const sql = base["text_to_sql"];
      const sqlEnabled = !!(
        sql &&
        typeof sql === "object" &&
        !Array.isArray(sql) &&
        (sql as Record<string, unknown>)["enabled"] === true
      );
      if (resolved.defaultMode === "sql" && !sqlEnabled)
        throw new Error(
          "此專案未啟用 SQL 查詢(text_to_sql.enabled 為關),預設模式不能選 SQL——政策 schema 禁止預設到一個自己停用的模式",
        );
      const nextPolicy = {
        ...base,
        default_mode: resolved.defaultMode,
        max_top_k: resolved.maxTopK,
        max_graph_hops: resolved.maxGraphHops,
      };
      const { data, error } = await api.PATCH("/projects/{project}", {
        params: { path: { project } },
        body: { config: { ...current, query_policy: nextPolicy } },
      });
      if (error) throw new Error(error.error.message);
      return { project: data.data, saved: resolved, created: !usable };
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["project", project] }),
  });
}
