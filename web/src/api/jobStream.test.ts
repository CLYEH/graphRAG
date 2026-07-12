import { afterEach, describe, expect, it, vi } from "vitest";

import { parseSse, streamJobEvents } from "./jobStream";
import { sseResponse } from "../test-utils";

import type { SseFrame } from "./jobStream";

describe("parseSse", () => {
  it("parses an event+data frame and consumes it", () => {
    const { frames, rest } = parseSse('event: job.update\ndata: {"progress":0.4}\n\n');
    expect(frames).toEqual([{ event: "job.update", data: '{"progress":0.4}' }]);
    expect(rest).toBe("");
  });

  it("holds back a frame split across chunks until its terminator arrives", () => {
    // the fiddly case: a network chunk can cut a frame anywhere, so an
    // un-terminated block must stay in `rest` rather than parse half an event
    const first = parseSse("event: job.update\nda");
    expect(first.frames).toEqual([]);
    const second = parseSse(first.rest + "ta: x\n\n");
    expect(second.frames).toEqual([{ event: "job.update", data: "x" }]);
    expect(second.rest).toBe("");
  });

  it("parses multiple frames and keeps the trailing partial", () => {
    const { frames, rest } = parseSse("data: a\n\ndata: b\n\ndata: c");
    expect(frames.map((f: SseFrame) => f.data)).toEqual(["a", "b"]);
    expect(rest).toBe("data: c");
  });

  it("defaults the event name and strips one leading data space", () => {
    const { frames } = parseSse("data:  x\n\n");
    expect(frames).toEqual([{ event: "message", data: " x" }]);
  });
});

describe("streamJobEvents", () => {
  afterEach(() => vi.restoreAllMocks());

  it("delivers each frame and percent-encodes the job id in the URL", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        sseResponse([
          'event: job.update\ndata: {"progress":0.4}\n\n',
          'event: job.done\ndata: {"progress":1}\n\n',
        ]),
      );
    vi.stubGlobal("fetch", fetchMock);

    const frames: SseFrame[] = [];
    await streamJobEvents("a/b", {
      signal: new AbortController().signal,
      onFrame: (f) => frames.push(f),
    });

    expect(String(fetchMock.mock.calls[0][0])).toContain("/jobs/a%2Fb/events");
    expect(frames.map((f) => f.event)).toEqual(["job.update", "job.done"]);
  });

  it("throws on a non-ok response so the caller fails loud", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("nope", { status: 500 })));
    await expect(
      streamJobEvents("x", { signal: new AbortController().signal, onFrame: () => {} }),
    ).rejects.toThrow(/HTTP 500/);
  });
});
