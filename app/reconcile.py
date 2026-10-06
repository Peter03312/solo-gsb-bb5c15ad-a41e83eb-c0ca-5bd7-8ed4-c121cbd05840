"""实测浓度对账（本轮新增）。

实验人员核对实际配液是否沿预检轨迹运行：先沿用原请求的逐项校验与模拟
（单容器 build_plan/simulate，双容器 build_transfer_plan/simulate_transfer），
再用**未四舍五入**的逐位置浓度与实测值比较：

- 对账位置：position=0 为初始态，正整数为该 seq 步骤**完成后**的状态；
- 该位置未出现的已知试剂按 0% 计（组分被倒空/转出/清洗后同样为 0）；
- 比较一律使用 runtime 的原始计算值（快照里 round(...,4) 的值不参与对账）；
- |实测 - 预测| ≤ tolerance_percentage_points 才通过。

任一观测引用未知步骤/容器/试剂、同位置（双容器还须同容器）同试剂重复、
实测浓度越界、容差非法或原流程无效时，整单拒绝，不返回任何部分对账。
"""
from __future__ import annotations

import math
from typing import Any

from . import errors
from .catalog import REAGENTS
from .runtime import VesselRuntime
from .transfer_validator import TransferPlan
from .validator import Plan, _detail

_EPS = 1e-9


# ---------------------------------------------------------------------------
# 未四舍五入的逐位置浓度重放（与既有引擎共用 VesselRuntime 簿记）
# ---------------------------------------------------------------------------
def replay_single_concentrations(plan: Plan) -> dict[int, dict[str, float]]:
    """重放单容器计划，返回 {position: {reagent_id: 未舍入浓度%}}。

    position 0 为初始态；之后每个计划步骤完成后一条（按 seq 执行顺序）。
    """
    rt = VesselRuntime(vessel_id="vessel", vessel_material=plan.vessel_material)
    out: dict[int, dict[str, float]] = {}

    rt.load_initial(plan.initial)
    out[0] = dict(rt.concentrations())

    for st in plan.steps:
        rt.advance(st.at_seconds)
        if st.op == "add":
            rt.apply_add(st.reagent_id, st.concentration, st.volume_ml)
        elif st.op == "dilute":
            rt.apply_dilute(st.volume_ml)
        elif st.op == "pour":
            rt.apply_pour(st.volume_ml)
        elif st.op == "clean":
            rt.apply_clean()
        # hold：仅推进时间，浓度不变
        out[st.seq] = dict(rt.concentrations())
    return out


def replay_transfer_concentrations(
    plan: TransferPlan,
) -> dict[int, dict[str, dict[str, float]]]:
    """重放双容器计划，返回 {position: {vessel_id: {reagent_id: 未舍入浓度%}}}。"""
    runtimes = [
        VesselRuntime(vessel_id=spec.vessel_id, vessel_material=spec.vessel_material) for spec in plan.vessels
    ]
    by_id = {rt.vessel_id: rt for rt in runtimes}

    for rt, spec in zip(runtimes, plan.vessels):
        rt.load_initial(spec.initial)
    out: dict[int, dict[str, dict[str, float]]] = {
        0: {rt.vessel_id: dict(rt.concentrations()) for rt in runtimes}
    }

    for st in plan.steps:
        for rt in runtimes:
            rt.advance(st.at_seconds)
        if st.op == "transfer":
            src = by_id[st.from_vessel]
            dst = by_id[st.to_vessel]
            moved = src.transfer_out(st.volume_ml)
            dst.transfer_in(moved, st.volume_ml)
        # hold：仅推进时间
        out[st.seq] = {rt.vessel_id: dict(rt.concentrations()) for rt in runtimes}
    return out


# ---------------------------------------------------------------------------
# 观测逐项校验
# ---------------------------------------------------------------------------
def _check_observations(
    observations: list[Any],
    *,
    valid_positions: set[int],
    known_vessels: set[str] | None,
) -> list[dict]:
    """校验观测列表；任何错误都会导致整单拒绝。"""
    errs: list[dict] = []
    seen: set[tuple] = set()
    for i, obs in enumerate(observations):
        pf = f"observations[{i}]"
        key_extra = (obs.vessel_id,) if known_vessels is not None else ()

        if obs.position not in valid_positions:
            errs.append(
                _detail(
                    errors.INVALID_POSITION,
                    "观测位置 {p} 不存在（position 须为 0=初始态 或已提交步骤序号之一）".format(
                        p=obs.position
                    ),
                    fld=f"{pf}.position",
                )
            )
        if obs.reagent_id not in REAGENTS:
            errs.append(
                _detail(
                    errors.UNKNOWN_REAGENT,
                    f"未知试剂编号：{obs.reagent_id}",
                    fld=f"{pf}.reagent_id",
                )
            )
        if not (0.0 <= obs.measured_concentration <= 100.0):
            errs.append(
                _detail(
                    errors.CONCENTRATION_OUT_OF_RANGE,
                    f"实测浓度须在 [0, 100] 区间内，收到 {obs.measured_concentration}",
                    fld=f"{pf}.measured_concentration",
                )
            )
        if known_vessels is not None and obs.vessel_id not in known_vessels:
            errs.append(
                _detail(
                    errors.UNKNOWN_VESSEL,
                    f"未知容器编号：{obs.vessel_id}",
                    fld=f"{pf}.vessel_id",
                )
            )

        dedup_key = (obs.position, obs.reagent_id, *key_extra)
        if dedup_key in seen:
            if known_vessels is not None:
                msg = (
                    f"容器 {obs.vessel_id} 位置 {obs.position} 对试剂 {obs.reagent_id} "
                    "的观测重复（同位置同容器同试剂仅允许一条）"
                )
            else:
                msg = f"位置 {obs.position} 对试剂 {obs.reagent_id} 的观测重复（同位置同试剂仅允许一条）"
            errs.append(_detail(errors.DUPLICATE_OBSERVATION, msg, fld=pf))
        else:
            seen.add(dedup_key)
    return errs


def _fail(details: list[dict]) -> None:
    if details:
        raise errors.ValidationFailure(details, message="对账请求被拒绝，未返回部分对账")


# ---------------------------------------------------------------------------
# 对账主流程
# ---------------------------------------------------------------------------
def _compare(
    observations: list[Any],
    predicted: dict[int, Any],
    *,
    tolerance: float,
    vessel_mode: bool,
    execution_order: list[int],
) -> dict:
    items: list[dict] = []
    rank = {seq: i for i, seq in enumerate(execution_order)}

    for i, obs in enumerate(observations):
        if vessel_mode:
            table = predicted[obs.position][obs.vessel_id]
        else:
            table = predicted[obs.position]
        pred = table.get(obs.reagent_id, 0.0)  # 该位置未出现的已知试剂按零计

        measured = obs.measured_concentration
        deviation = measured - pred
        abs_dev = abs(deviation)
        item = {
            "index": i,
            "position": obs.position,
            "reagent_id": obs.reagent_id,
            "measured_concentration": measured,
            "predicted_concentration": pred,      # 未四舍五入的计算值
            "deviation": deviation,
            "abs_deviation": abs_dev,
            "within_tolerance": abs_dev <= tolerance + _EPS,
        }
        if vessel_mode:
            item["vessel_id"] = obs.vessel_id
        items.append(item)

    violations = [it for it in items if not it["within_tolerance"]]
    first: dict | None = None
    if violations:
        # 按步骤执行顺序（初始态最前）；同位置按观测提交顺序（index 升序）
        top = min(violations, key=lambda it: (rank[it["position"]], it["index"]))
        first = {
            k: top[k]
            for k in (
                "index",
                "position",
                "reagent_id",
                "measured_concentration",
                "predicted_concentration",
                "abs_deviation",
                *(() if not vessel_mode else ("vessel_id",)),
            )
        }

    return {
        "items": items,
        "first_violation": first,
        "within_tolerance": not violations,
    }


def _tolerance_errors(tolerance: float) -> list[dict]:
    if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
        return [
            _detail(
                errors.INVALID_TOLERANCE,
                f"容差须为有限非负数字（百分点），收到 {tolerance}",
                fld="tolerance_percentage_points",
            )
        ]
    return []


def reconcile_single(req: Any, plan: Plan) -> dict[str, Any]:
    """单容器对账：调用方须已通过 build_plan 原请求校验。"""
    errs = _tolerance_errors(req.tolerance_percentage_points)

    valid_positions = {0, *(st.seq for st in plan.steps)}
    errs.extend(_check_observations(req.observations, valid_positions=valid_positions, known_vessels=None))
    _fail(errs)

    predicted = replay_single_concentrations(plan)
    return _compare(
        req.observations,
        predicted,
        tolerance=req.tolerance_percentage_points,
        vessel_mode=False,
        execution_order=[0, *(st.seq for st in plan.steps)],
    )


def reconcile_transfer(req: Any, plan: TransferPlan) -> dict[str, Any]:
    """双容器对账：调用方须已通过 build_transfer_plan 原请求校验。"""
    errs = _tolerance_errors(req.tolerance_percentage_points)

    known_vessels = {spec.vessel_id for spec in plan.vessels}
    valid_positions = {0, *(st.seq for st in plan.steps)}
    errs.extend(
        _check_observations(req.observations, valid_positions=valid_positions, known_vessels=known_vessels)
    )
    _fail(errs)

    predicted = replay_transfer_concentrations(plan)
    return _compare(
        req.observations,
        predicted,
        tolerance=req.tolerance_percentage_points,
        vessel_mode=True,
        execution_order=[0, *(st.seq for st in plan.steps)],
    )
