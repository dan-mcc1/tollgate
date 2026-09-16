"""Reading a Gemini stream as it passes through, without holding on to it.

Token counts only appear inside the streamed events, so the gateway has to understand
what it is relaying. It must not keep the stream, though: bytes are handed onward
immediately and only a small parse buffer is retained.

Gemini streams in two shapes, and the gateway relays whichever the caller asked for:

  ?alt=sse   server-sent events: `data: {...}` separated by blank lines
  otherwise  one JSON array, streamed: `[{...}` then `,{...}` then `]`

Chunks arrive at network boundaries, so an event can be split across two chunks, and a
multi-byte character can be split down the middle. Both parsers cope with that.
"""

import json
from codecs import getincrementaldecoder
from collections.abc import Iterator
from typing import Any

# A single event is a few KB at most. If this much text arrives without a complete event
# in it, the stream isn't what we think it is: stop parsing rather than buffer forever.
# Relaying continues; the usage row just keeps whatever counts it had.
MAX_BUFFER_BYTES = 256 * 1024

SSE_DATA_PREFIX = "data:"


class StreamScanner:
    """Bytes in, decoded JSON objects out. Holds at most one incomplete event."""

    def __init__(self, *, sse: bool) -> None:
        self.sse = sse
        self.overflowed = False
        self._buffer = ""
        self._decoder = getincrementaldecoder("utf-8")(errors="replace")
        self._json = json.JSONDecoder()

    def feed(self, chunk: bytes) -> Iterator[dict[str, Any]]:
        if self.overflowed:
            return
        self._buffer += self._decoder.decode(chunk)
        parser = self._take_sse_events if self.sse else self._take_array_items
        yield from parser()
        if len(self._buffer) > MAX_BUFFER_BYTES:
            self.overflowed = True
            self._buffer = ""

    def _take_sse_events(self) -> Iterator[dict[str, Any]]:
        # Events end with a blank line. Servers may use \n or \r\n.
        while True:
            end = min(
                (i for i in (self._buffer.find("\n\n"), self._buffer.find("\r\n\r\n")) if i != -1),
                default=-1,
            )
            if end == -1:
                return
            event, self._buffer = self._buffer[:end], self._buffer[end:].lstrip("\r\n")
            for line in event.splitlines():
                if line.startswith(SSE_DATA_PREFIX):
                    payload = self._loads(line[len(SSE_DATA_PREFIX) :].strip())
                    if payload is not None:
                        yield payload

    def _take_array_items(self) -> Iterator[dict[str, Any]]:
        # The array's own punctuation, then one complete object at a time.
        while True:
            self._buffer = self._buffer.lstrip("[,\r\n\t ")
            if not self._buffer or self._buffer[0] != "{":
                return
            try:
                payload, end = self._json.raw_decode(self._buffer)
            except ValueError:
                return  # the object is still arriving
            self._buffer = self._buffer[end:]
            if isinstance(payload, dict):
                yield payload

    def _loads(self, text: str) -> dict[str, Any] | None:
        try:
            payload: Any = json.loads(text)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
