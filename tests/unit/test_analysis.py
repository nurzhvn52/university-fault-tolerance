import pytest

from analysis import fmea, model
from analysis.results import charge_outages, predict_outage


def test_availability_series_and_parallel():
    a = model.availability(mttf_h=99, mttr_h=1)

    assert a == pytest.approx(0.99)
    assert model.series(a, a) == pytest.approx(0.9801)
    assert model.parallel(a, a) == pytest.approx(0.9999)


def test_quadrupling_mtbf_and_quartering_mttr_give_the_same_availability():
    # Week 2 example: MTBF 500 h, MTTR 20 h.
    result = model.sensitivity(500, 20)

    assert result["base"] == pytest.approx(500 / 520)
    assert result["mttf_x4"] == pytest.approx(result["mttr_div4"])


def test_redundancy_beats_the_baseline_and_the_gateway_dominates():
    baseline, ft = model.baseline_model(), model.ft_model()

    assert ft["availability"] > baseline["availability"]
    unavailability = {name: 1 - a for name, a in ft["blocks"].items()}
    assert max(unavailability, key=unavailability.get) == "gateway"


def test_fault_tree_top_event_matches_the_block_diagram():
    ft = model.ft_model()
    tree = model.fault_tree(ft)

    assert tree["p_top"] == pytest.approx(1 - ft["availability"])
    assert tree["cut_set_order"]["gateway"] == 1
    assert tree["cut_set_order"]["database pair"] == 2


def test_fmea_rpn_is_the_product_of_the_scores():
    rows = fmea.table()

    assert all(
        r["rpn_baseline"] == r["baseline"][0] * r["baseline"][1] * r["baseline"][2] for r in rows
    )
    assert rows == sorted(rows, key=lambda r: -r["rpn_baseline"])


def test_outage_prediction_uses_self_recovery_when_faster_than_the_repair():
    model_ = {
        "crash": {"self_recovery_s": 2.0, "after_repair_s": 3.0},
        "kill": {"self_recovery_s": None, "after_repair_s": 2.5},
    }

    assert predict_outage(model_, "crash", duration_s=30) == 2.0
    assert predict_outage(model_, "crash", duration_s=0.5) == 2.0
    assert predict_outage(model_, "kill", duration_s=30) == 32.5


def test_outages_are_charged_to_the_last_fault_before_them():
    faults = [(10.0, "crash"), (50.0, "kill"), (90.0, "hang")]
    # the first outage starts a second before the kill is logged (bucket alignment);
    # the second one belongs to the kill as well; the crash caused none
    spans = [(49.0, 20.0), (75.0, 3.0), (90.0, 2.0)]
    assert charge_outages(faults, spans) == [0.0, 23.0, 2.0]
