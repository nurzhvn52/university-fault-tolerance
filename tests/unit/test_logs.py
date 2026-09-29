import json
import logging

from common.logs import JsonFormatter


def make_record(**extra) -> logging.LogRecord:
    record = logging.makeLogRecord({"name": "test", "levelname": "INFO", "msg": "payment_captured"})
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_log_line_is_json_with_event_and_extra_fields():
    formatter = JsonFormatter(service="payment", node="a")

    entry = json.loads(formatter.format(make_record(payment_id=7, amount="1000.00")))

    assert entry["event"] == "payment_captured"
    assert entry["service"] == "payment"
    assert entry["node"] == "a"
    assert entry["payment_id"] == 7
    assert entry["amount"] == "1000.00"
    assert entry["ts"].endswith("+00:00")


def test_standard_record_attributes_are_not_copied():
    formatter = JsonFormatter(service="payment", node="a")

    entry = json.loads(formatter.format(make_record()))

    assert "args" not in entry
    assert "msg" not in entry
    assert "exc" not in entry
