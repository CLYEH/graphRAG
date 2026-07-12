import { apiBaseUrl, apiToken } from "./client";

import type { components } from "./schema";

export type JobEvent = components["schemas"]["JobEvent"];

export interface SseFrame {
  event: string;
  data: string;
}

// Splits accumulated SSE text into complete frames (blank-line separated),
// returning the parsed frames plus the leftover partial text — so a frame split
// across network chunks is held back until its terminator arrives. Kept pure and
// exported so the framing (the fiddly part) is unit-tested directly.
export function parseSse(buffer: string): { frames: SseFrame[]; rest: string } {
  const frames: SseFrame[] = [];
  let rest = buffer;
  let sep: number;
  while ((sep = rest.indexOf("\n\n")) !== -1) {
    const block = rest.slice(0, sep);
    rest = rest.slice(sep + 2);
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length > 0) frames.push({ event, data: data.join("\n") });
  }
  return { frames, rest };
}

// Opens the job event stream over fetch (not EventSource — which can't attach the
// bearer token) and invokes onFrame for each SSE frame until the stream ends
// (job.done / job.failed close it) or the signal aborts. Throws on a non-OK
// response so the caller fails loud instead of showing a dead progress bar.
export async function streamJobEvents(
  jobId: string,
  opts: { signal: AbortSignal; onFrame: (frame: SseFrame) => void },
): Promise<void> {
  const res = await fetch(`${apiBaseUrl}/jobs/${encodeURIComponent(jobId)}/events`, {
    headers: { Authorization: `Bearer ${apiToken}`, Accept: "text/event-stream" },
    signal: opts.signal,
  });
  if (!res.ok || !res.body) throw new Error(`Event stream failed (HTTP ${res.status}).`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
    const parsed = parseSse(buffer);
    buffer = parsed.rest;
    for (const frame of parsed.frames) opts.onFrame(frame);
  }
}
