"""The stream parser: events split across chunks, both Gemini stream shapes."""

from tollgate.proxy.sse import MAX_BUFFER_BYTES, StreamScanner


def test_sse_event_split_across_chunks_is_parsed_once() -> None:
    scanner = StreamScanner(sse=True)
    whole = b'data: {"n": 1}\r\n\r\n'

    assert list(scanner.feed(whole[:9])) == []  # nothing complete yet
    assert list(scanner.feed(whole[9:])) == [{"n": 1}]


def test_several_events_in_one_chunk() -> None:
    scanner = StreamScanner(sse=True)

    events = list(scanner.feed(b'data: {"n": 1}\n\ndata: {"n": 2}\n\ndata: {"n":'))

    assert events == [{"n": 1}, {"n": 2}]


def test_multibyte_character_split_across_chunks() -> None:
    scanner = StreamScanner(sse=True)
    payload = 'data: {"text": "héllo"}\r\n\r\n'.encode()
    split = payload.index(b"\xc3") + 1  # between the two bytes of "é"

    events = list(scanner.feed(payload[:split])) + list(scanner.feed(payload[split:]))

    assert events == [{"text": "héllo"}]


def test_json_array_stream_is_parsed_object_by_object() -> None:
    scanner = StreamScanner(sse=False)

    first = list(scanner.feed(b'[{"n": 1}'))
    second = list(scanner.feed(b',{"n": 2}'))
    third = list(scanner.feed(b"]"))

    assert (first, second, third) == ([{"n": 1}], [{"n": 2}], [])


def test_non_json_data_is_skipped_not_fatal() -> None:
    scanner = StreamScanner(sse=True)

    events = list(scanner.feed(b': keepalive\r\n\r\ndata: not json\r\n\r\ndata: {"n": 1}\r\n\r\n'))

    assert events == [{"n": 1}]


def test_parser_stops_buffering_if_the_stream_makes_no_sense() -> None:
    scanner = StreamScanner(sse=True)

    list(scanner.feed(b"x" * (MAX_BUFFER_BYTES + 1)))  # no event delimiter, ever

    assert scanner.overflowed
    assert list(scanner.feed(b'data: {"n": 1}\r\n\r\n')) == []  # parsing stopped
