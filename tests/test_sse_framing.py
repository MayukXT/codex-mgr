"""Stream framing limits and exact data across network chunk boundaries."""

import pytest

from codex_mgr.core.adapters.sse import encode_event, json_events, server_events
from codex_mgr.core.adapters.tools import ProtocolError


def test_exact_line_limit_handles_a_crlf_split_across_chunks():
    events = list(server_events([b"data: {}\r", b"\n\r", b"\n"], max_line_bytes=8))
    assert len(events) == 1 and events[0].data == "{}"


def test_event_limit_counts_crlf_and_the_terminating_blank_line_exactly():
    source = b"data: {}\r\n\r\n"
    for boundary in range(len(source) + 1):
        chunks = [source[:boundary], source[boundary:]]
        with pytest.raises(ProtocolError, match="event exceeds"):
            list(server_events(chunks, max_event_bytes=len(source) - 1))
        assert [event.data for event in server_events(chunks, max_event_bytes=len(source))] == [
            "{}"
        ]


def test_partial_final_comments_cannot_exceed_the_event_limit():
    with pytest.raises(ProtocolError, match="event exceeds"):
        list(server_events([b": synthetic comment"], max_event_bytes=10))


def test_memoryview_size_uses_bytes_and_noncontiguous_views_decode_safely():
    source = memoryview(b"data: {}\n\n").cast("H")
    with pytest.raises(ProtocolError, match="total size"):
        list(server_events([source], max_total_bytes=6))
    spaced = memoryview(b"d a t a :   { } \n \n ")[::2]
    assert [event.data for event in json_events([spaced])] == [{}]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_unicode_bom_comments_metadata_and_multiline_json_survive_every_byte_boundary(newline):
    lines = [
        b"\xef\xbb\xbf: keepalive",
        b"id: ignored",
        b"retry: 100",
        b"event: synthetic",
        'data: {"text":"Exact café text",'.encode(),
        b'data: "number":2}',
        b"",
        b"",
    ]
    source = newline.join(lines)
    for boundary in range(len(source) + 1):
        events = list(json_events([source[:boundary], source[boundary:]]))
        assert len(events) == 1
        assert events[0].name == "synthetic"
        assert events[0].data == {"text": "Exact café text", "number": 2}
    events = list(json_events([bytes([value]) for value in source]))
    assert events[0].data["text"] == "Exact café text"


def test_data_loses_only_the_single_optional_sse_space_and_event_type_resets():
    source = b"event: first\ndata:  Exact spaces  \ndata:Next line\n\ndata: last\n\n"
    events = list(server_events([source]))
    assert events[0].data == " Exact spaces  \nNext line" and events[0].name == "first"
    assert events[1].data == "last" and events[1].name is None


def test_empty_data_and_keepalives_do_not_invent_json_and_done_is_explicit():
    events = list(json_events([b": ping\n\ndata:\n\ndata: [DONE]\n\n: final comment"]))
    assert len(events) == 1 and events[0].done and events[0].data is None


@pytest.mark.parametrize(
    "source",
    [
        b"data: {",
        b"data: {}\n",
        b"data: {}\r",
        b"event: incomplete",
        b"data: \xff\n\n",
        b"data: \xc3",
        b'data: {"same":1,"same":2}\n\n',
        b"data: []\n\n",
        b'data: {"value":NaN}\n\n',
        b"data: synthetic-private\n\n",
    ],
)
def test_truncated_invalid_or_ambiguous_events_fail_with_fixed_messages(source):
    with pytest.raises(ProtocolError) as failure:
        list(json_events([source]))
    assert "synthetic-private" not in str(failure.value)


@pytest.mark.parametrize(
    "limits,source",
    [
        ({"max_line_bytes": 10}, b"data: 1234567890\n\n"),
        ({"max_line_bytes": 10}, b"data: 1234567890"),
        ({"max_event_bytes": 20}, b"data: first\ndata: second\n\n"),
        ({"max_total_bytes": 15}, b"data: {}\n\ndata: {}\n\n"),
        ({"max_events": 1}, b"data: {}\n\ndata: {}\n\n"),
    ],
)
def test_line_event_total_and_count_limits_apply_across_chunks(limits, source):
    with pytest.raises(ProtocolError, match="limit"):
        list(server_events([bytes([value]) for value in source], **limits))


@pytest.mark.parametrize("limit", [0, -1, True, "10"])
def test_invalid_limits_fail_before_consuming_the_stream(limit):
    consumed = []

    def source():
        consumed.append(True)
        yield b"data: {}\n\n"

    with pytest.raises(ProtocolError):
        list(server_events(source(), max_events=limit))
    assert not consumed


def test_payloads_and_server_event_names_are_hidden_from_representations():
    event = next(server_events([b"event: synthetic-private\ndata: synthetic-private\n\n"]))
    assert "synthetic-private" not in repr(event)
    decoded = next(json_events([b'data: {"secret":"synthetic-private"}\n\n']))
    assert "synthetic-private" not in repr(decoded)


def test_outgoing_json_newlines_cannot_inject_a_second_event():
    source = {
        "type": "response.output_text.delta",
        "delta": "Exact café\n\nevent: fake\ndata: fake",
    }
    encoded = encode_event(source)
    events = list(json_events([encoded]))
    assert len(events) == 1 and events[0].data == source
    assert events[0].name == source["type"]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.created\n\ndata: synthetic-private"},
        {"type": None},
        [],
        {"type": "response.created", "value": float("nan")},
        {"type": "response.created", "value": "\ud800"},
    ],
)
def test_outgoing_invalid_types_json_and_text_fail_without_raw_values(event):
    with pytest.raises(ProtocolError) as failure:
        encode_event(event)
    assert "synthetic-private" not in str(failure.value)


def test_outgoing_event_size_is_bounded():
    with pytest.raises(ProtocolError, match="size limit"):
        encode_event({"type": "response.created", "value": "x" * 200}, max_bytes=128)


def test_framing_is_lazy_and_does_not_consume_future_network_chunks():
    consumed = []

    def source():
        consumed.append("first")
        yield b"data: {}\n\n"
        consumed.append("second")
        yield b"data: {}\n\n"

    events = server_events(source())
    assert next(events).data == "{}" and consumed == ["first"]
    events.close()
    assert consumed == ["first"]
