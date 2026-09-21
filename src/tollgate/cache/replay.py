"""Giving a cached answer back in whichever shape the caller asked for.

The cache stores one thing: the provider's complete response object, exactly as a
non-streaming call returns it. Both directions of this file exist because callers do not
all want it that way.

  assemble()      many stream events  ->  the one response object that is stored
  stream_events() the one response object  ->  many events, for a streaming caller

That round trip is what lets a single entry serve both methods (see keys.cache_key: the
method is not part of the key). A streaming caller can be answered from an entry a
non-streaming caller paid for, and the other way round.

**A replayed stream is a real stream.** Same event framing, same `responseId`, same
`modelVersion`, same `finishReason` on the last event, and cumulative `usageMetadata`
throughout - so an SDK parsing it takes exactly the path it takes on a miss. The
intermediate token counts are interpolated from how much text has been emitted, which is
what a running total is; only the final event's counts are the stored, exact ones, and
the final event is the one anything downstream actually reads.

**What a replay does not do is pretend to be slow.** There are no inter-event delays. A
cached stream arrives as fast as the caller can read it, which is the entire point, and
is the one way a client that is looking for it can tell.
"""

import json
import re
from collections.abc import Iterator
from typing import Any

from tollgate.cache.exact import CachedResponse

# Words per replayed event. Chosen to match what the mock upstream emits, so a replayed
# stream and a live one are chunked alike and a test comparing them compares like to like.
WORDS_PER_EVENT = 6

# A word and the whitespace that follows it, so re-joining the pieces reproduces the
# original text byte for byte. Splitting on whitespace and re-joining with " " would not.
WORD = re.compile(r"\S+\s*")

CACHE_HEADER = "x-tollgate-cache"


def candidate_of(response: dict[str, Any]) -> dict[str, Any]:
    candidates = response.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return {}


def parts_of(response: dict[str, Any]) -> list[dict[str, Any]]:
    content = candidate_of(response).get("content")
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def usage_of(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usageMetadata")
    return usage if isinstance(usage, dict) else {}


def token_counts(response: dict[str, Any]) -> tuple[int, int, int]:
    """(input, output, thoughts) as the provider reported them, zero where it did not."""
    usage = usage_of(response)

    def count(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return count("promptTokenCount"), count("candidatesTokenCount"), count("thoughtsTokenCount")


# --- many events in, one response out -------------------------------------------------------


def assemble(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The complete response a sequence of stream events describes, or None if it is not one.

    The last event is the template: it is the one carrying the final `usageMetadata` and
    the `finishReason`, which is why a stream that ended early cannot be assembled and is
    not offered here. Its text parts are replaced by the concatenation of every event's,
    which is what the caller actually received.
    """
    if not events:
        return None
    assembled = dict(events[-1])

    text = "".join(
        part["text"]
        for event in events
        for part in parts_of(event)
        if isinstance(part.get("text"), str)
    )
    # Parts that are not text - a function call, inline data - are kept in the order they
    # arrived. A stream splits text across events and does not split these.
    other = [part for event in events for part in parts_of(event) if "text" not in part]

    candidate = dict(candidate_of(assembled))
    content = candidate.get("content")
    content = dict(content) if isinstance(content, dict) else {}
    content["parts"] = ([{"text": text}] if text else []) + other
    content.setdefault("role", "model")
    candidate["content"] = content
    assembled["candidates"] = [candidate]
    return assembled


# --- one response in, many events out -------------------------------------------------------


def event_parts(response: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The response's parts, split into one list per event to emit.

    Text is split at word boundaries. Every other kind of part travels whole, in a final
    event of its own: a function call cut in half is not a function call.
    """
    text = "".join(part["text"] for part in parts_of(response) if isinstance(part.get("text"), str))
    other = [part for part in parts_of(response) if "text" not in part]

    words = WORD.findall(text)
    chunks = [
        "".join(words[i : i + WORDS_PER_EVENT]) for i in range(0, len(words), WORDS_PER_EVENT)
    ]
    events: list[list[dict[str, Any]]] = [[{"text": chunk}] for chunk in chunks]
    if other:
        events.append(other)
    # A response with no content at all still owes the caller one event, so the final
    # usage and finish reason have somewhere to travel.
    return events or [[{"text": ""}]]


def interpolated_usage(final: dict[str, Any], emitted: int, total: int) -> dict[str, Any]:
    """Cumulative usage partway through, scaled by how much text has gone out.

    The prompt count is exact from the first event - it describes the request, which is
    fully known before a single token is generated. The output and thinking counts are
    the running totals a live stream reports, so here they are scaled to the fraction of
    the text emitted so far. The last event does not come through this function: it
    carries the stored figures, unaltered, and it is the one the ledger reads.
    """
    if total <= 0:
        return dict(final)
    share = emitted / total
    scaled = dict(final)
    for name in ("candidatesTokenCount", "thoughtsTokenCount"):
        value = final.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            scaled[name] = int(value * share)
    prompt = final.get("promptTokenCount")
    if isinstance(prompt, int):
        scaled["totalTokenCount"] = (
            prompt
            + int(scaled.get("candidatesTokenCount", 0) or 0)
            + int(scaled.get("thoughtsTokenCount", 0) or 0)
        )
    return scaled


def events_for(response: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The response as the sequence of events a streaming caller would have received."""
    groups = event_parts(response)
    final_usage = usage_of(response)
    finish = candidate_of(response).get("finishReason")
    total_characters = sum(
        len(part.get("text", "")) for group in groups for part in group if "text" in part
    )
    emitted = 0

    for index, parts in enumerate(groups):
        last = index == len(groups) - 1
        emitted += sum(len(part.get("text", "")) for part in parts if "text" in part)

        candidate: dict[str, Any] = {"content": {"parts": parts, "role": "model"}, "index": 0}
        if last and finish is not None:
            candidate["finishReason"] = finish
        event: dict[str, Any] = {"candidates": [candidate]}
        if final_usage:
            event["usageMetadata"] = (
                dict(final_usage)
                if last
                else interpolated_usage(final_usage, emitted, total_characters)
            )
        for key in ("modelVersion", "responseId"):
            if key in response:
                event[key] = response[key]
        yield event


def stream_events(entry: CachedResponse, *, sse: bool) -> Iterator[bytes]:
    """The cached response, framed the way this caller asked for it.

    Gemini offers two framings and the gateway relays whichever was requested, so a
    replay has to produce both: server-sent events for `?alt=sse`, and one streamed JSON
    array otherwise. proxy/sse.py parses the same two shapes on the way in.
    """
    first = True
    for event in events_for(entry.response):
        data = json.dumps(event, ensure_ascii=False)
        if sse:
            yield f"data: {data}\r\n\r\n".encode()
        else:
            yield (("[" if first else "\r\n,") + data).encode()
        first = False
    if not sse:
        yield b"[]" if first else b"]"


def response_media_type(sse: bool) -> str:
    return "text/event-stream" if sse else "application/json"


def hit_headers(status: str) -> dict[str, str]:
    """What a hit adds to the response.

    One header, and nothing in the body: the request and response bodies stay
    byte-compatible with the provider, which is the constraint the whole gateway is
    built around. A caller that wants to know where its answer came from can read the
    header; one that does not, cannot tell.
    """
    return {CACHE_HEADER: status}
