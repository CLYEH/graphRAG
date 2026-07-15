import { useState } from "react";

import {
  DEFAULT_QUERY_POLICY,
  chunkingFromConfig,
  ontologyFromConfig,
  policyFromConfig,
  useProject,
  useSaveChunking,
  useSaveOntology,
  useSaveQueryPolicy,
} from "../api/queries";
import { isPathAddressable, useActiveProject } from "../project/projectRoute";
import "./Settings.css";

import type { OntologyDraft, OntologyEdits, QueryMode } from "../api/queries";

// UXB1 設定頁 (DESIGN §6/§21): the three config blocks an operator actually
// tunes — 知識類型 (ontology), 切塊 (chunking), 問答安全 (query_policy) — as
// forms over the existing PATCH. Two disciplines inherited verbatim from FE2
// (see queries.ts): every save spreads a FRESH config read (the PATCH
// replaces the whole column), and the page fails CLOSED while the config
// read is loading/failed (a form without the real config saves a wipe).
//
// Unlike Clean, this page does NOT unmount its forms during revalidation:
// each section holds a local draft (null = clean, tracking the server
// baseline), and an early return on isFetching would destroy a sibling
// section's unsaved draft every time one section's save invalidates the
// project query (the class-20 family: state must live where renders can't
// kill it). Fail-closed instead gates the AFFORDANCE — every save button
// disables while the read is refetching or errored.

const MODE_LABELS: Record<QueryMode, string> = {
  hybrid: "混合(hybrid)",
  semantic: "語意檢索(semantic)",
  graph: "圖譜查詢(graph)",
  sql: "SQL 查詢(sql)",
  global: "全域摘要(global)",
};
const MODES: readonly QueryMode[] = ["hybrid", "semantic", "graph", "sql", "global"];

function message(error: unknown): string {
  return error instanceof Error ? error.message : "unknown error";
}

export function Settings() {
  const project = useActiveProject();

  if (project === undefined) return <p className="settings__line">Unknown project.</p>;
  if (!isPathAddressable(project))
    return (
      <p className="settings__line settings__line--error">
        Project &quot;{project}&quot; isn&apos;t addressable over the API — its key contains
        &quot;/&quot; or is &quot;.&quot; / &quot;..&quot;, which a URL path segment can&apos;t
        carry.
      </p>
    );

  return <SettingsBody project={project} />;
}

function SettingsBody({ project }: { project: string }) {
  const proj = useProject(project);

  if (proj.isPending) return <p className="settings__line">載入專案設定中…</p>;
  if (proj.isError && proj.data === undefined)
    return (
      <p className="settings__line settings__line--error">無法載入專案設定:{message(proj.error)}</p>
    );

  const config = (proj.data?.config ?? {}) as Record<string, unknown>;
  // fail CLOSED at the affordance: a refetching or refetch-failed read means
  // the baselines on screen may not be the server's truth — saves wait
  const locked = proj.isFetching || proj.isError;

  return (
    <div className="settings">
      <h1>設定</h1>
      <p className="settings__muted">
        這裡的設定會在<strong>下一次建置 / 下一次問答</strong>生效,不影響已上線的知識庫。
      </p>
      {proj.isError && (
        <p className="settings__line settings__line--error">
          設定重新讀取失敗,為避免存出過時內容,儲存已暫停:{message(proj.error)}
        </p>
      )}
      <OntologySection project={project} config={config} locked={locked} />
      <ChunkingSection project={project} config={config} locked={locked} />
      <PolicySection project={project} config={config} locked={locked} />
    </div>
  );
}

type SectionProps = { project: string; config: Record<string, unknown>; locked: boolean };

// ---- 知識類型 (ontology) ----------------------------------------------------

function ChipEditor({
  label,
  rawKey,
  values,
  onChange,
  disabled,
}: {
  label: string;
  rawKey: string;
  values: string[];
  onChange: (next: string[]) => void;
  disabled: boolean;
}) {
  const [text, setText] = useState("");
  function add() {
    const v = text.trim();
    if (v === "" || values.includes(v)) return;
    onChange([...values, v]);
    setText("");
  }
  return (
    <div className="settings__field">
      <span className="settings__label">
        {label}
        <span className="settings__rawkey">({rawKey})</span>
      </span>
      <div className="settings__chips">
        {values.map((v) => (
          <span key={v} className="settings__chip">
            {v}
            <button
              type="button"
              aria-label={`移除 ${v}`}
              disabled={disabled}
              onClick={() => onChange(values.filter((x) => x !== v))}
            >
              ×
            </button>
          </span>
        ))}
        {values.length === 0 && <span className="settings__muted">(未設定)</span>}
      </div>
      <div className="settings__chipadd">
        <input
          type="text"
          aria-label={`新增${label}`}
          value={text}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <button type="button" disabled={disabled || text.trim() === ""} onClick={add}>
          加入{label}
        </button>
      </div>
    </div>
  );
}

function OntologySection({ project, config, locked }: SectionProps) {
  const base = ontologyFromConfig(config);
  const save = useSaveOntology(project);
  // per-field edits (undefined = untouched, tracking the baseline) — a save
  // must send ONLY what was edited so untouched fields resolve from the fresh
  // block, never revert a concurrent change (Codex #79 R7, the chunking rule)
  const [edits, setEdits] = useState<OntologyEdits>({});
  const [savedKey, setSavedKey] = useState<string | null>(null);

  const shown: OntologyDraft = {
    entityTypes: edits.entityTypes ?? base.entityTypes,
    relationTypes: edits.relationTypes ?? base.relationTypes,
    proposalPolicy: edits.proposalPolicy ?? base.proposalPolicy,
  };
  const dirty = Object.keys(edits).length > 0;
  const key = (d: OntologyDraft) =>
    JSON.stringify([d.entityTypes, d.relationTypes, d.proposalPolicy]);
  const bothEmpty = shown.entityTypes.length === 0 && shown.relationTypes.length === 0;
  const oneSided = (shown.entityTypes.length === 0) !== (shown.relationTypes.length === 0);
  // a PRESENT-but-malformed persisted block (a curl-era typo'd proposal_policy,
  // an unknown key, an empty vocabulary) is itself unsaved state: the form
  // shows the salvaged-clean version, which lives nowhere on the server, so
  // saving it is a repair — enable even without an edit (Codex #79 R4, the
  // ontology sibling of the R1/R2 policy rule)
  const malformed = base.malformed;
  const savedStands = savedKey !== null && savedKey === key(shown);

  function runSave() {
    save.mutate(
      {
        ...(edits.entityTypes !== undefined ? { entityTypes: edits.entityTypes } : {}),
        ...(edits.relationTypes !== undefined ? { relationTypes: edits.relationTypes } : {}),
        ...(edits.proposalPolicy !== undefined ? { proposalPolicy: edits.proposalPolicy } : {}),
      },
      {
        onSuccess: (r) => {
          setSavedKey(key(r.saved));
          setEdits({});
        },
      },
    );
  }

  return (
    <section className="settings__section" aria-label="知識類型">
      <h2>知識類型</h2>
      <p className="settings__muted">
        建置時,系統只會抽取這份詞彙表裡的實體與關係;表外的新類型依「新類型處理」決定去向。
        清空兩個清單=移除整份詞彙表(僅適用於純結構化資料的專案——含文件的建置沒有詞彙表會失敗)。
      </p>
      <ChipEditor
        label="實體類型"
        rawKey="entity_types"
        values={shown.entityTypes}
        disabled={locked || save.isPending}
        onChange={(next) => setEdits({ ...edits, entityTypes: next })}
      />
      <ChipEditor
        label="關係類型"
        rawKey="relation_types"
        values={shown.relationTypes}
        disabled={locked || save.isPending}
        onChange={(next) => setEdits({ ...edits, relationTypes: next })}
      />
      <div className="settings__field">
        <span className="settings__label">
          新類型處理<span className="settings__rawkey">(proposal_policy)</span>
        </span>
        {(
          [
            ["review", "審核後採用(建議)"],
            ["auto", "自動採用"],
          ] as const
        ).map(([value, label]) => (
          <label key={value} className="settings__radio">
            <input
              type="radio"
              name={`ontology-policy-${project}`}
              checked={shown.proposalPolicy === value}
              disabled={locked || save.isPending}
              onChange={() => setEdits({ ...edits, proposalPolicy: value })}
            />
            {label}
          </label>
        ))}
      </div>
      {malformed && (
        <p className="settings__line settings__line--notice">
          此專案目前的知識類型設定不符規範(欄位缺漏、含未知欄位、或新類型處理值無效),建置會失敗。
          下方是依現有內容整理後的版本,確認後儲存即可修正。
        </p>
      )}
      {oneSided && (
        <p className="settings__line settings__line--error">
          實體類型與關係類型必須同時提供,或同時清空(移除整份詞彙表)。
        </p>
      )}
      <div className="settings__actions">
        <button
          type="button"
          disabled={locked || save.isPending || oneSided || (!malformed && !dirty)}
          onClick={runSave}
        >
          {save.isPending
            ? "儲存中…"
            : bothEmpty && (base.present || malformed)
              ? "儲存(移除整份詞彙表)"
              : "儲存知識類型"}
        </button>
        {dirty && !save.isPending && (
          <button type="button" onClick={() => setEdits({})}>
            還原未存的修改
          </button>
        )}
      </div>
      {save.isError && (
        <p className="settings__line settings__line--error">儲存失敗:{message(save.error)}</p>
      )}
      {savedStands && !save.isPending && <p className="settings__line">已儲存。</p>}
    </section>
  );
}

// ---- 切塊 (chunking) --------------------------------------------------------

function ChunkingSection({ project, config, locked }: SectionProps) {
  const configured = chunkingFromConfig(config);
  const save = useSaveChunking(project);
  const [maxCharsText, setMaxCharsText] = useState("");
  const [overlapText, setOverlapText] = useState("");
  const [savedPair, setSavedPair] = useState<{ max: number; overlap: number } | null>(null);

  // Clean 頁同款:空欄=沿用目前設定;鏡射 chunk_text 的配對規則(class-15
  // 判準:存壞的配對要到下次建置的 config load 才會炸,這裡先講人話)
  const maxChars = maxCharsText.trim() === "" ? undefined : Number(maxCharsText);
  const overlap = overlapText.trim() === "" ? undefined : Number(overlapText);
  const effectiveMax = maxChars ?? configured.max_chars;
  const effectiveOverlap = overlap ?? configured.overlap;
  const pairError =
    !Number.isInteger(effectiveMax) || effectiveMax < 1
      ? "每塊字元上限(max_chars)必須是 ≥ 1 的整數"
      : !Number.isInteger(effectiveOverlap) ||
          effectiveOverlap < 0 ||
          effectiveOverlap >= effectiveMax
        ? `重疊字元數必須滿足 0 ≤ 重疊 < 上限(目前 ${effectiveOverlap} / ${effectiveMax})`
        : null;
  const savedStands =
    savedPair !== null && savedPair.max === effectiveMax && savedPair.overlap === effectiveOverlap;

  function runSave() {
    save.mutate(
      {
        ...(maxChars !== undefined ? { max_chars: maxChars } : {}),
        ...(overlap !== undefined ? { overlap } : {}),
      },
      {
        onSuccess: (r) => {
          setSavedPair({ max: r.pair.max_chars, overlap: r.pair.overlap });
          setMaxCharsText("");
          setOverlapText("");
        },
      },
    );
  }

  return (
    <section className="settings__section" aria-label="切塊">
      <h2>切塊</h2>
      <p className="settings__muted">
        文件在建置時會切成小段落再索引。目前設定:每塊上限 {configured.max_chars} 字元、重疊{" "}
        {configured.overlap} 字元。想先看切出來的效果,請到「清洗」頁預覽。
      </p>
      <label className="settings__field">
        <span className="settings__label">
          每塊字元上限<span className="settings__rawkey">(max_chars)</span>
        </span>
        <input
          type="number"
          value={maxCharsText}
          placeholder={String(configured.max_chars)}
          disabled={locked || save.isPending}
          onChange={(e) => setMaxCharsText(e.target.value)}
        />
      </label>
      <label className="settings__field">
        <span className="settings__label">
          重疊字元數<span className="settings__rawkey">(overlap)</span>
        </span>
        <input
          type="number"
          value={overlapText}
          placeholder={String(configured.overlap)}
          disabled={locked || save.isPending}
          onChange={(e) => setOverlapText(e.target.value)}
        />
      </label>
      {pairError !== null && <p className="settings__line settings__line--error">{pairError}</p>}
      <div className="settings__actions">
        <button
          type="button"
          disabled={
            locked ||
            save.isPending ||
            pairError !== null ||
            (maxChars === undefined && overlap === undefined)
          }
          onClick={runSave}
        >
          {save.isPending ? "儲存中…" : `儲存 ${effectiveMax}/${effectiveOverlap} 到專案設定`}
        </button>
      </div>
      {save.isError && (
        <p className="settings__line settings__line--error">儲存失敗:{message(save.error)}</p>
      )}
      {savedStands && !save.isPending && <p className="settings__line">已儲存。</p>}
    </section>
  );
}

// ---- 問答安全 (query_policy) ------------------------------------------------

function PolicySection({ project, config, locked }: SectionProps) {
  const pol = policyFromConfig(config);
  const save = useSaveQueryPolicy(project);
  // per-field edits (undefined = untouched, tracking the baseline) — a save
  // must send ONLY what was edited so untouched fields resolve from the fresh
  // block, never revert a concurrent change (Codex #79 R6, the chunking rule)
  const [edits, setEdits] = useState<{
    defaultMode?: QueryMode;
    maxTopKText?: string;
    maxGraphHopsText?: string;
  }>({});
  const [savedOps, setSavedOps] = useState<string | null>(null);

  const shown = {
    defaultMode: edits.defaultMode ?? pol.defaultMode,
    maxTopKText: edits.maxTopKText ?? String(pol.maxTopK),
    maxGraphHopsText: edits.maxGraphHopsText ?? String(pol.maxGraphHops),
  };
  const dirty = Object.keys(edits).length > 0;
  const maxTopK = Number(shown.maxTopKText);
  const maxGraphHops = Number(shown.maxGraphHopsText);
  const fieldError =
    !Number.isInteger(maxTopK) || maxTopK < 1
      ? "單次檢索筆數上限(max_top_k)必須是 ≥ 1 的整數"
      : !Number.isInteger(maxGraphHops) || maxGraphHops < 1
        ? "圖譜跳數上限(max_graph_hops)必須是 ≥ 1 的整數"
        : null;
  const key = (o: { defaultMode: QueryMode; maxTopK: number; maxGraphHops: number }) =>
    JSON.stringify([o.defaultMode, o.maxTopK, o.maxGraphHops]);
  const savedStands =
    savedOps !== null &&
    savedOps === key({ defaultMode: shown.defaultMode, maxTopK, maxGraphHops });

  function runSave() {
    save.mutate(
      {
        edits: {
          ...(edits.defaultMode !== undefined ? { defaultMode: edits.defaultMode } : {}),
          ...(edits.maxTopKText !== undefined ? { maxTopK } : {}),
          ...(edits.maxGraphHopsText !== undefined ? { maxGraphHops } : {}),
        },
        salvaged: {
          defaultMode: pol.defaultMode,
          maxTopK: pol.maxTopK,
          maxGraphHops: pol.maxGraphHops,
        },
      },
      {
        onSuccess: (r) => {
          setSavedOps(key(r.saved));
          setEdits({});
        },
      },
    );
  }

  return (
    <section className="settings__section" aria-label="問答安全">
      <h2>問答安全</h2>
      {!pol.present && (
        <p className="settings__line settings__line--notice">
          此專案尚未設定問答安全政策,問答功能目前無法使用。儲存後會以安全預設範本建立 (SQL/Cypher
          查詢均停用)。
        </p>
      )}
      {pol.malformed && (
        <p className="settings__line settings__line--notice">
          此專案的問答安全政策不符規範——欄位缺漏、含未知欄位或值超出允許範圍
          (可能是先前手動設定所致),問答會持續被拒絕。儲存會以安全預設範本
          <strong>重建整份政策</strong>,只保留這一頁的三個欄位設定。
        </p>
      )}
      <label className="settings__field">
        <span className="settings__label">
          預設問答模式<span className="settings__rawkey">(default_mode)</span>
        </span>
        <select
          value={shown.defaultMode}
          disabled={locked || save.isPending}
          onChange={(e) => setEdits({ ...edits, defaultMode: e.target.value as QueryMode })}
        >
          {MODES.map((m) => (
            <option
              key={m}
              value={m}
              disabled={m === "sql" && !pol.sqlEnabled}
              title={
                m === "sql" && !pol.sqlEnabled
                  ? "此專案未啟用 SQL 查詢(text_to_sql.enabled 為關)"
                  : undefined
              }
            >
              {MODE_LABELS[m]}
            </option>
          ))}
        </select>
      </label>
      <label className="settings__field">
        <span className="settings__label">
          單次檢索筆數上限<span className="settings__rawkey">(max_top_k)</span>
        </span>
        <input
          type="number"
          value={shown.maxTopKText}
          disabled={locked || save.isPending}
          onChange={(e) => setEdits({ ...edits, maxTopKText: e.target.value })}
        />
      </label>
      <label className="settings__field">
        <span className="settings__label">
          圖譜跳數上限<span className="settings__rawkey">(max_graph_hops)</span>
        </span>
        <input
          type="number"
          value={shown.maxGraphHopsText}
          disabled={locked || save.isPending}
          onChange={(e) => setEdits({ ...edits, maxGraphHopsText: e.target.value })}
        />
      </label>
      <p className="settings__field">
        <span className="settings__label">
          必附來源<span className="settings__rawkey">(require_sources)</span>
        </span>
        固定開啟——每個回答都必須附來源引用(凍結契約,不可關閉)。
      </p>
      {fieldError !== null && <p className="settings__line settings__line--error">{fieldError}</p>}
      <div className="settings__actions">
        <button
          type="button"
          // a MISSING or MALFORMED policy is itself unsaved state: what this
          // save would write (the template) lives nowhere on the server, so
          // saving AS-IS is meaningful — requiring an arbitrary edit first
          // would gate the whole query feature behind a pointless field
          // change (Codex #79 R1; R2 extends it to partial curl-era blocks)
          disabled={
            locked ||
            save.isPending ||
            (pol.present && !pol.malformed && !dirty) ||
            fieldError !== null
          }
          onClick={runSave}
        >
          {save.isPending
            ? "儲存中…"
            : pol.malformed
              ? "以預設範本重建並儲存"
              : pol.present
                ? "儲存問答安全設定"
                : "以預設範本建立並儲存"}
        </button>
        {dirty && !save.isPending && (
          <button type="button" onClick={() => setEdits({})}>
            還原未存的修改
          </button>
        )}
      </div>
      {save.isError && (
        <p className="settings__line settings__line--error">儲存失敗:{message(save.error)}</p>
      )}
      {savedStands && !save.isPending && <p className="settings__line">已儲存。</p>}
      <details className="settings__advanced">
        <summary>進階:SQL 查詢防護(text_to_sql,唯讀)</summary>
        <pre>{JSON.stringify(pol.sqlBlock ?? DEFAULT_QUERY_POLICY.text_to_sql, null, 2)}</pre>
      </details>
      <details className="settings__advanced">
        <summary>進階:圖譜查詢防護(text_to_cypher,唯讀)</summary>
        <pre>{JSON.stringify(pol.cypherBlock ?? DEFAULT_QUERY_POLICY.text_to_cypher, null, 2)}</pre>
      </details>
    </section>
  );
}
