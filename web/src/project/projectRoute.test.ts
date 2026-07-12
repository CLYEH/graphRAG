import { describe, expect, it } from "vitest";

import { decodeProjectSegment, encodeProjectSegment, isPathAddressable } from "./projectRoute";

describe("project route encoding", () => {
  // The contract allows any non-empty project key; every one must survive the
  // round trip and produce a segment with no URL-reserved char and no dot
  // segment (the three hazards Codex #65 walked through: reserved chars, then
  // dot segments, then — structurally — everything).
  const keys = [
    "acme",
    "a/b", // reserved: path separator
    "a?b#c", // reserved: query/hash
    ".", // dot segment
    "..", // dot segment
    "a b", // space
    "日本語プロジェクト", // unicode
    "%2F", // already-encoded-looking
  ];

  it.each(keys)("round-trips %j", (key) => {
    const seg = encodeProjectSegment(key);
    expect(seg).toMatch(/^[A-Za-z0-9_-]+$/); // base64url alphabet only
    expect(seg).not.toBe(".");
    expect(seg).not.toBe("..");
    expect(decodeProjectSegment(seg)).toBe(key);
  });

  it("returns undefined for a segment that is not valid encoding", () => {
    // atob rejects the reserved chars; a bad URL must yield "unknown", not throw
    expect(decodeProjectSegment("!!!not-base64!!!")).toBeUndefined();
  });

  it("returns undefined for base64 that decodes to invalid UTF-8", () => {
    // "_w" is valid base64url (byte 0xFF) but not valid UTF-8; fatal decoding
    // must collapse it to the single "unknown" signal, not a U+FFFD garbage key
    expect(decodeProjectSegment("_w")).toBeUndefined();
  });
});

describe("isPathAddressable", () => {
  // The complete un-addressable set (derived from the transport): dot-segment
  // tokens normalize away, and "/"-bearing keys hit the single-segment {project}
  // route as a decoded slash (404). Everything else percent-encodes to a
  // surviving non-dot segment.
  it.each([".", "..", "a/b", "/", "a/", "a/.."])("rejects the un-addressable key %j", (key) => {
    expect(isPathAddressable(key)).toBe(false);
  });

  it.each(["acme", "a.b", ".hidden", "..leading", "%2e", "a?b", "a b", "日本語"])(
    "accepts the addressable key %j",
    (key) => {
      expect(isPathAddressable(key)).toBe(true);
    },
  );
});
