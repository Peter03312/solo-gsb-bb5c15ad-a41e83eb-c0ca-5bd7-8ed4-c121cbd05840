"""实测浓度对账 API 端到端测试（单容器 /reconcile 与双容器 /transfer-reconcile）。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# 公共构造
# ---------------------------------------------------------------------------
def safe_single_request():
    return {
        "vessel_material": "hdpe",
        "temperature_c": 22,
        "initial_liquid": [{"reagent_id": "nacl", "concentration": 10.0, "volume_ml": 100.0}],
        "steps": [
            {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 20.0, "volume_ml": 100.0},
            {"seq": 2, "op": "dilute", "volume_ml": 100.0, "at_seconds": 10},
        ],
        "contact_allowances": [],
    }


def forbidden_request():
    return {
        "vessel_material": "glass",
        "temperature_c": 22,
        "initial_liquid": [{"reagent_id": "naocl", "concentration": 10.0, "volume_ml": 100.0}],
        "steps": [
            {"seq": 1, "op": "add", "reagent_id": "hcl", "concentration": 30.0, "volume_ml": 50.0},
        ],
        "contact_allowances": [],
    }


def safe_transfer_request():
    return {
        "vessels": [
            {
                "vessel_id": "V1",
                "vessel_material": "hdpe",
                "initial_liquid": [
                    {"reagent_id": "nacl", "concentration": 10.0, "volume_ml": 100.0}
                ],
            },
            {"vessel_id": "V2", "vessel_material": "hdpe", "initial_liquid": []},
        ],
        "temperature_c": 20.0,
        "steps": [
            {
                "seq": 1,
                "op": "transfer",
                "from_vessel": "V1",
                "to_vessel": "V2",
                "volume_ml": 40.0,
                "at_seconds": 30.0,
            }
        ],
        "contact_allowances": [],
    }


def post_single(payload):
    return client.post("/api/v1/reconcile", json=payload)


def post_transfer(payload):
    return client.post("/api/v1/transfer-reconcile", json=payload)


# ---------------------------------------------------------------------------
# 单容器：基本对账
# ---------------------------------------------------------------------------
class TestSingleReconcile:
    def test_all_within_tolerance(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.2,
            "observations": [
                {"position": 0, "reagent_id": "nacl", "measured_concentration": 10.05},
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.1},
                {"position": 2, "reagent_id": "nacl", "measured_concentration": 9.9},
            ],
        }
        d = post_single(payload).json()
        assert d["within_tolerance"] is True
        assert d["first_violation"] is None
        assert [o["within_tolerance"] for o in d["observations"]] == [True, True, True]
        # 带符号偏差
        assert d["observations"][2]["deviation"] == pytest.approx(-0.1)
        assert d["observations"][2]["abs_deviation"] == pytest.approx(0.1)

    def test_predicted_concentrations_match_math(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 0, "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.0},
                {"position": 2, "reagent_id": "nacl", "measured_concentration": 10.0},
            ],
        }
        d = post_single(payload).json()
        preds = [o["predicted_concentration"] for o in d["observations"]]
        assert preds == pytest.approx([10.0, 15.0, 10.0])

    def test_uses_unrounded_values(self):
        # naocl 10%×100mL + hcl 30%×50mL：naocl 预测 1000/150 = 6.6666...（快照只 round 到 6.6667）
        payload = {
            "request": forbidden_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 1, "reagent_id": "naocl", "measured_concentration": 6.6667},
            ],
        }
        d = post_single(payload).json()
        item = d["observations"][0]
        # 若误用四舍五入值 6.6667，偏差会是 0；这里必须是与未舍入值的差
        assert item["abs_deviation"] == pytest.approx(6.6667 - 20.0 / 3.0, abs=1e-12)
        assert item["within_tolerance"] is False

    def test_absent_known_reagent_counts_as_zero(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 0, "reagent_id": "koh", "measured_concentration": 0.0},
                {"position": 1, "reagent_id": "koh", "measured_concentration": 0.0},
            ],
        }
        d = post_single(payload).json()
        assert all(o["predicted_concentration"] == 0.0 for o in d["observations"])
        assert d["within_tolerance"] is True

    def test_traced_absent_zero(self):
        # 倒空后该位置未出现的已知试剂按零计
        req = {
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "initial_liquid": [{"reagent_id": "nacl", "concentration": 10.0, "volume_ml": 100.0}],
            "steps": [{"seq": 1, "op": "pour", "volume_ml": 100.0}],
        }
        payload = {
            "request": req,
            "tolerance_percentage_points": 0.0,
            "observations": [{"position": 1, "reagent_id": "nacl", "measured_concentration": 0.0}],
        }
        d = post_single(payload).json()
        assert d["observations"][0]["predicted_concentration"] == 0.0
        assert d["within_tolerance"] is True

    def test_boundary_exactly_at_tolerance_passes(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.5,
            "observations": [
                {"position": 0, "reagent_id": "nacl", "measured_concentration": 10.5},
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 14.5},
            ],
        }
        d = post_single(payload).json()
        assert d["within_tolerance"] is True

    def test_violation_marked_and_item_order_preserved(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 2, "reagent_id": "nacl", "measured_concentration": 9.0},
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 14.0},
            ],
        }
        d = post_single(payload).json()
        # 逐条结果仍按提交顺序
        assert [o["position"] for o in d["observations"]] == [2, 1]
        assert [o["within_tolerance"] for o in d["observations"]] == [False, False]

    def test_earliest_violation_follows_execution_order_then_submission(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 2, "reagent_id": "nacl", "measured_concentration": 9.0},   # 后执行
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 14.0},  # 先执行
            ],
        }
        d = post_single(payload).json()
        fv = d["first_violation"]
        assert fv["position"] == 1
        assert fv["index"] == 1
        assert fv["abs_deviation"] == pytest.approx(1.0)

    def test_initial_position_zero_is_earliest(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.0},
                {"position": 0, "reagent_id": "nacl", "measured_concentration": 9.0},
            ],
        }
        d = post_single(payload).json()
        assert d["first_violation"]["position"] == 0
        assert d["first_violation"]["index"] == 1

    def test_same_position_observation_submission_order_tiebreak(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 99.0},
                {"position": 1, "reagent_id": "koh", "measured_concentration": 1.0},
            ],
        }
        d = post_single(payload).json()
        assert d["first_violation"]["index"] == 0
        assert d["first_violation"]["reagent_id"] == "nacl"

    def test_no_observations_returns_precheck_only(self):
        payload = {"request": safe_single_request(), "tolerance_percentage_points": 1.0, "observations": []}
        r = post_single(payload)
        assert r.status_code == 200
        d = r.json()
        assert d["within_tolerance"] is True
        assert d["observations"] == []
        assert d["precheck"]["safe"] is True
        assert d["precheck"]["steps"]

    def test_observations_field_optional(self):
        payload = {"request": safe_single_request(), "tolerance_percentage_points": 1.0}
        r = post_single(payload)
        assert r.status_code == 200
        assert r.json()["observations"] == []

    def test_precheck_blocked_still_returns_reconciliation(self):
        # 原预检有风险（禁配）不等于对账失败；风险结果照常返回
        payload = {
            "request": forbidden_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [{"position": 1, "reagent_id": "hcl", "measured_concentration": 10.0}],
        }
        d = post_single(payload).json()
        assert d["precheck"]["blocked"] is True
        assert d["precheck"]["findings"][0]["rule_id"] == "F01"
        assert d["within_tolerance"] is True

    def test_precheck_block_is_full_original_response(self):
        payload = {"request": safe_single_request(), "tolerance_percentage_points": 1.0, "observations": []}
        d = post_single(payload).json()["precheck"]
        direct = client.post("/api/v1/precheck", json=safe_single_request()).json()
        assert d == direct

    def test_unordered_steps_reconciled_by_execution_order(self):
        req = {
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "initial_liquid": [],
            "steps": [
                {"seq": 2, "op": "dilute", "volume_ml": 100.0, "at_seconds": 10},
                {"seq": 1, "op": "add", "reagent_id": "nacl", "concentration": 20.0, "volume_ml": 100.0},
            ],
        }
        payload = {
            "request": req,
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 2, "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 20.0},
            ],
        }
        d = post_single(payload).json()
        assert d["within_tolerance"] is True


# ---------------------------------------------------------------------------
# 单容器：整单拒绝
# ---------------------------------------------------------------------------
class TestSingleRejections:
    def test_unknown_step(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [{"position": 99, "reagent_id": "nacl", "measured_concentration": 1.0}],
        }
        r = post_single(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"INVALID_POSITION"}

    def test_position_negative(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [{"position": -1, "reagent_id": "nacl", "measured_concentration": 1.0}],
        }
        assert post_single(payload).status_code == 400

    def test_unknown_reagent(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [{"position": 0, "reagent_id": "unobtainium", "measured_concentration": 1.0}],
        }
        r = post_single(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"UNKNOWN_REAGENT"}

    def test_duplicate_same_position_same_reagent(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.0},
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.1},
            ],
        }
        r = post_single(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"DUPLICATE_OBSERVATION"}

    def test_same_reagent_different_positions_allowed(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 1, "reagent_id": "nacl", "measured_concentration": 15.0},
                {"position": 2, "reagent_id": "nacl", "measured_concentration": 10.0},
            ],
        }
        assert post_single(payload).status_code == 200

    @pytest.mark.parametrize("val", [-0.01, -1.0])
    def test_negative_tolerance_rejected_with_code(self, val):
        payload = {"request": safe_single_request(), "tolerance_percentage_points": val, "observations": []}
        r = post_single(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"INVALID_TOLERANCE"}

    def test_nan_inf_tolerance_malformed(self):
        for raw in ("NaN", "Infinity", "-Infinity"):
            body = (
                '{"request": '
                + json.dumps(safe_single_request())
                + ', "tolerance_percentage_points": '
                + raw
                + ', "observations": []}'
            )
            r = client.post("/api/v1/reconcile", content=body, headers={"content-type": "application/json"})
            assert r.status_code == 400
            assert all(x["code"] == "MALFORMED_REQUEST" for x in r.json()["details"])

    @pytest.mark.parametrize("val", [100.01, -0.01])
    def test_measured_out_of_range(self, val):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [{"position": 0, "reagent_id": "nacl", "measured_concentration": val}],
        }
        r = post_single(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"CONCENTRATION_OUT_OF_RANGE"}

    def test_measured_boundaries_zero_and_hundred_ok(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 100.0,
            "observations": [
                {"position": 0, "reagent_id": "koh", "measured_concentration": 0.0},
                {"position": 0, "reagent_id": "hcl", "measured_concentration": 100.0},
            ],
        }
        assert post_single(payload).status_code == 200

    def test_invalid_original_flow_rejects_whole_order(self):
        bad_req = {
            "vessel_material": "hdpe",
            "temperature_c": 20,
            "initial_liquid": [],
            "steps": [{"seq": 0, "op": "hold", "at_seconds": 10}],
        }
        payload = {
            "request": bad_req,
            "tolerance_percentage_points": 0.1,
            "observations": [{"position": 0, "reagent_id": "nacl", "measured_concentration": 1.0}],
        }
        r = post_single(payload)
        assert r.status_code == 400
        body = r.json()
        # 原流程错误码原样透出，且不返回任何对账字段
        assert any(x["code"] == "INVALID_SEQ" for x in body["details"])
        assert "observations" not in body and "precheck" not in body

    def test_multiple_errors_collected_no_partial_result(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": -1.0,
            "observations": [
                {"position": 99, "reagent_id": "nacl", "measured_concentration": 1.0},
                {"position": 1, "reagent_id": "ghost", "measured_concentration": 200.0},
            ],
        }
        r = post_single(payload)
        assert r.status_code == 400
        codes = {x["code"] for x in r.json()["details"]}
        assert {"INVALID_POSITION", "UNKNOWN_REAGENT", "CONCENTRATION_OUT_OF_RANGE", "INVALID_TOLERANCE"} <= codes

    def test_extra_field_rejected(self):
        payload = {
            "request": safe_single_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [],
            "bogus": 1,
        }
        assert post_single(payload).status_code == 400

    def test_bool_inputs_rejected(self):
        for payload in (
            {"request": safe_single_request(), "tolerance_percentage_points": True, "observations": []},
            {
                "request": safe_single_request(),
                "tolerance_percentage_points": 0.1,
                "observations": [{"position": True, "reagent_id": "nacl", "measured_concentration": 1.0}],
            },
            {
                "request": safe_single_request(),
                "tolerance_percentage_points": 0.1,
                "observations": [{"position": 0, "reagent_id": "nacl", "measured_concentration": True}],
            },
        ):
            assert post_single(payload).status_code == 400


# ---------------------------------------------------------------------------
# 双容器
# ---------------------------------------------------------------------------
class TestTransferReconcile:
    def test_both_vessels_predicted(self):
        payload = {
            "request": safe_transfer_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 0, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 0, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 0.0},
                {"position": 1, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 1, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 10.0},
            ],
        }
        d = post_transfer(payload).json()
        assert d["within_tolerance"] is True
        assert all("vessel_id" in o for o in d["observations"])
        assert d["precheck"]["blocked"] is False

    def test_partial_transfer_keeps_concentration_source_gains_target(self):
        payload = {
            "request": safe_transfer_request(),
            "tolerance_percentage_points": 0.0,
            "observations": [
                {"position": 1, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 1, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 10.0},
            ],
        }
        d = post_transfer(payload).json()
        v1, v2 = d["observations"]
        assert v1["predicted_concentration"] == pytest.approx(10.0)
        assert v2["predicted_concentration"] == pytest.approx(10.0)

    def test_earliest_violation_across_steps(self):
        req = safe_transfer_request()
        req["steps"].append({"seq": 2, "op": "hold", "at_seconds": 120.0})
        payload = {
            "request": req,
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 2, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 5.0},
                {"position": 1, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 5.0},
            ],
        }
        d = post_transfer(payload).json()
        assert d["first_violation"]["position"] == 1
        assert d["first_violation"]["vessel_id"] == "V2"
        assert d["first_violation"]["index"] == 1

    def test_unknown_vessel_rejected(self):
        payload = {
            "request": safe_transfer_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 1, "vessel_id": "V9", "reagent_id": "nacl", "measured_concentration": 10.0}
            ],
        }
        r = post_transfer(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"UNKNOWN_VESSEL"}

    def test_missing_vessel_id_malformed(self):
        payload = {
            "request": safe_transfer_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [{"position": 1, "reagent_id": "nacl", "measured_concentration": 10.0}],
        }
        assert post_transfer(payload).status_code == 400

    def test_duplicate_same_vessel_position_reagent(self):
        payload = {
            "request": safe_transfer_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 1, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 1, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.1},
            ],
        }
        r = post_transfer(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"DUPLICATE_OBSERVATION"}

    def test_same_position_reagent_in_different_vessels_allowed(self):
        payload = {
            "request": safe_transfer_request(),
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 1, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 10.0},
                {"position": 1, "vessel_id": "V2", "reagent_id": "nacl", "measured_concentration": 10.0},
            ],
        }
        assert post_transfer(payload).status_code == 200

    def test_precheck_block_matches_transfer_endpoint(self):
        payload = {"request": safe_transfer_request(), "tolerance_percentage_points": 1.0, "observations": []}
        d = post_transfer(payload).json()["precheck"]
        direct = client.post("/api/v1/transfer-precheck", json=safe_transfer_request()).json()
        assert d == direct

    def test_invalid_original_transfer_flow_rejects_whole_order(self):
        bad_req = safe_transfer_request()
        bad_req["steps"][0]["volume_ml"] = 9999.0
        payload = {
            "request": bad_req,
            "tolerance_percentage_points": 0.1,
            "observations": [
                {"position": 1, "vessel_id": "V1", "reagent_id": "nacl", "measured_concentration": 1.0}
            ],
        }
        r = post_transfer(payload)
        assert r.status_code == 400
        codes = {x["code"] for x in r.json()["details"]}
        assert "TRANSFER_EXCEEDS_VOLUME" in codes

    def test_negative_tolerance_transfer(self):
        payload = {"request": safe_transfer_request(), "tolerance_percentage_points": -0.5, "observations": []}
        r = post_transfer(payload)
        assert r.status_code == 400
        assert {x["code"] for x in r.json()["details"]} == {"INVALID_TOLERANCE"}

    def test_no_observations_transfer(self):
        payload = {"request": safe_transfer_request(), "tolerance_percentage_points": 0.3, "observations": []}
        d = post_transfer(payload).json()
        assert d["within_tolerance"] is True
        assert d["observations"] == []
        assert d["precheck"]["final_states"]
