from novelagent.trace.normalized_trajectory import validate_trajectory_payload


def _event(event_id="evt_a", trace_id="tr_a", turn=2):
    return {
        "event_id": event_id,
        "trace_id": trace_id,
        "trace_turn": turn,
    }


def test_validates_and_derives_trajectory_provenance():
    payload = {
        "trajectory": [{
            "role": "user",
            "content": "保留这个设定。",
            "timestamp": "2026-09-22T12:00:00+08:00",
        }],
        "provenance": [{
            "record_index": 0,
            "trace_id": "model-must-not-decide-this",
            "turn_id": "wrong",
            "turn_no": 99,
            "source_event_ids": ["evt_a"],
        }],
    }

    result = validate_trajectory_payload(payload, [_event()])

    assert result["provenance"] == [{
        "record_index": 0,
        "trace_id": "tr_a",
        "turn_id": "tr_a:2",
        "turn_no": 2,
        "source_event_ids": ["evt_a"],
    }]


def test_rejects_unbound_or_schema_extended_records():
    unbound = {
        "trajectory": [{
            "role": "user", "content": "内容",
            "timestamp": "2026-09-22T12:00:00Z",
        }],
        "provenance": [],
    }
    extended = {
        "trajectory": [{
            "role": "user", "content": "内容",
            "timestamp": "2026-09-22T12:00:00Z", "trace_id": "tr_a",
        }],
        "provenance": [{"record_index": 0, "source_event_ids": ["evt_a"]}],
    }

    assert validate_trajectory_payload(unbound, [_event()]) == {}
    assert validate_trajectory_payload(extended, [_event()]) == {}
