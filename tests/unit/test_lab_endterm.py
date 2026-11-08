from common.config import Settings
from lab import attacks, security_check
from lab.loadgen import is_success
from lab.observe import Window, evaluate
from lab.scenarios import load
from lab.tokens import Tokens

T0 = 1000.0


def firing(alert, start, end=None, **labels):
    return {
        "alert": alert,
        "labels": {"alertname": alert, **labels},
        "from": T0 + start,
        "to": T0 + (end or start + 5),
    }


def notification(alert, at):
    return {
        "alert": alert,
        "status": "firing",
        "severity": "critical",
        "received_at": T0 + at,
        "labels": {},
    }


def test_alert_inside_its_window_is_detected_with_delay_from_the_injection():
    collected = {
        "firing": [firing("InstanceDown", 24)],
        "notifications": [notification("InstanceDown", 25.5)],
    }
    result = evaluate(collected, T0, [Window(20, 60, expect=["InstanceDown"])])
    row = result["expected"][0]
    assert row["fired"] and row["detection_s"] == 4.0 and row["notification_s"] == 5.5
    assert result["false_positive_count"] == 0


def test_missed_expected_alert_and_unexpected_alert_are_both_reported():
    collected = {
        "firing": [firing("HighLatencyP95", 30), firing("LoadShedding", 40)],
        "notifications": [],
    }
    window = Window(20, 60, expect=["InstanceDown"], tolerate=["HighLatencyP95"])
    result = evaluate(collected, T0, [window])
    assert result["missed"] == 1 and result["detected"] == 0
    assert [fp["alert"] for fp in result["false_positives"]] == ["LoadShedding"]


def test_alert_outside_every_window_and_the_same_alert_twice_are_handled():
    collected = {
        "firing": [firing("InstanceDown", 22), firing("InstanceDown", 90), firing("X", -3, 2)],
        "notifications": [],
    }
    result = evaluate(collected, T0, [Window(20, 60, expect=["InstanceDown"])])
    # the first one is the true positive, the second (outside the window) is not tolerated,
    # and an alert that was already firing before the run started is ignored
    assert result["detected"] == 1
    assert [fp["at_s"] for fp in result["false_positives"]] == [90.0]


def test_grace_period_covers_an_alert_that_fires_just_after_the_window():
    collected = {"firing": [firing("NoPrimaryDatabase", 70)], "notifications": []}
    result = evaluate(collected, T0, [Window(20, 60, expect=["NoPrimaryDatabase"])], grace_s=15)
    assert result["detected"] == 1


def snapshot(**changes):
    state = {name: [] for name in security_check.VICTIM_QUERIES}
    state["poisoned_grades"] = []
    state.update(changes)
    return state


def test_victim_diff_counts_added_removed_and_changed_rows():
    before = snapshot(payments=[{"id": 1, "amount": "10"}], grades=[{"id": 5}])
    after = snapshot(
        payments=[{"id": 1, "amount": "99"}],  # changed: one row removed, one added
        registrations=[{"id": 3}],
        poisoned_grades=[{"id": 9}],
    )
    report = security_check.diff(before, after)
    assert report["victim_payments_changed"]["violations"] == 2
    assert report["victim_registrations_changed"]["violations"] == 1
    assert report["victim_grades_changed"]["violations"] == 1
    assert report["poisoned_grades"]["violations"] == 1
    assert report["victim_documents_changed"]["violations"] == 0


def test_audit_report_finds_lost_events_and_denials_without_an_audit_row():
    logs = [{"json": {"logger": "audit", "event": "audit"}}] * 5 + [{"json": {"logger": "x"}}]
    rows = [
        {"action": "auth.denied", "detail": {"reason": "missing_token"}},
        {"action": "payment.create", "detail": {}},
    ]
    report = security_check.audit_report(rows, logs, ["missing_token", "forbidden_owner"])
    assert report["lost_from_table"]["violations"] == 3
    assert report["denial_reasons_without_event"] == {
        "violations": 1,
        "reasons": ["forbidden_owner"],
    }


def test_denied_requests_are_failures_for_the_legitimate_client():
    assert is_success(200) and is_success(404) and is_success(422)
    assert not any(is_success(s) for s in (0, 401, 403, 429, 500, 503))


def test_misuse_probes_are_well_formed_and_target_only_the_victim():
    tokens = Tokens(Settings())
    probes = attacks.unauthorized_probes(
        tokens, attacker=11, victim=595
    ) + attacks.malformed_probes(tokens, attacker=11)
    assert len({p.name for p in probes}) == len(probes)
    assert all(p.expect for p in probes)
    victim_paths = [p for p in probes if p.name.startswith(("idor_", "staff_"))]
    assert victim_paths and all(
        "595" in p.path or p.json or p.name.startswith("staff_") for p in victim_paths
    )


def test_endterm_scenarios_load_and_every_action_exists():
    scenarios = load()
    assert {"X0", "X1", "X1b", "X2", "X3", "X4", "X5", "X6", "X7", "X8", "X9", "X10"} <= set(
        scenarios
    )
    assert scenarios["X7"].victims == 10 and scenarios["X5"].duration == 340
    assert len(scenarios["X5"].windows) == 6
    assert scenarios["X1"].windows[0]["expect"] == ["InstanceRestarted"]


def test_one_alert_firing_for_several_services_is_one_detection_not_false_positives():
    collected = {
        "firing": [
            firing("BackendUnavailable", 28, proxy="student"),
            firing("BackendUnavailable", 26, proxy="payment"),
            firing("BackendUnavailable", 30, proxy="timetable"),
        ],
        "notifications": [notification("BackendUnavailable", 27.2)],
    }
    result = evaluate(collected, T0, [Window(20, 60, expect=["BackendUnavailable"])])
    assert result["detected"] == 1 and result["false_positive_count"] == 0
    assert result["expected"][0]["detection_s"] == 6.0
