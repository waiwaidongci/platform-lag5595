"""固定采样网格上的配对 Pearson 互相关及缺测排查。

声级计走时不准时，分析在首尾对时偏差区间枚举的匀速漂移场景上进行：
声级读数校正后就近落到风机采样格（半格往后取），网格外或撞格按缺测；
各场景分别选峰，全部场景同档才建议复测。
"""

from datetime import datetime, timedelta
from math import fsum, hypot
from statistics import fmean

from . import clock
from .spec import SPEC

_CHANNELS = (("fan_hz", "风机频率"), ("sound_dba", "站台声级"))


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("请填写本地时间 YYYY-MM-DD HH:MM:SS") from None
    if result.isoformat(sep=" ") != value.replace("T", " ") or result.tzinfo is not None:
        raise ValueError("请填写本地时间 YYYY-MM-DD HH:MM:SS，精确到整秒")
    return result


def validate_rows(rows):
    """与 CSV 导入共用校验，返回可序列化且按时间排序的记录。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("请导入至少一条站台采样记录。")
    readings = []
    seen = set()
    sound_seen = set()
    equipment = None
    for index, row in enumerate(rows, 1):
        context = f"第 {index} 条站台采样"
        if not isinstance(row, dict):
            raise ValueError(f"{context}格式无效。")
        record = {}
        for field in SPEC["fields"]:
            key, label = field["key"], field["label"]
            value = row.get(key)
            if field["type"] == "string":
                if value is None or (isinstance(value, str) and not value.strip()):
                    if field.get("required", True):
                        raise ValueError(f"{context}的「{label}」不能为空。")
                    record[key] = None
                    continue
                if not isinstance(value, str) or len(value.strip()) > field.get("max_length", 300):
                    raise ValueError(f"{context}的「{label}」须为 1 至 {field.get('max_length', 300)} 个字符。")
                value = value.strip()
            elif value is not None and (
                type(value) not in (int, float) or not field["min"] <= value <= field["max"]
            ):
                raise ValueError(f"{context}的「{label}」须为 {field['min']} 至 {field['max']} 的数值或留空。")
            record[key] = value
        try:
            instant = _timestamp(record["timestamp"])
        except ValueError as error:
            raise ValueError(f"{context}时间无效。{error}。") from None
        record["timestamp"] = instant.isoformat(sep=" ")
        if instant in seen:
            raise ValueError(f"{context}的时间 {record['timestamp']} 重复，请保留每个时刻唯一的一行。")
        seen.add(instant)
        # 声级计读数时刻留空时与采样时间一致；填写时同样必须精确到整秒且唯一。
        if record.get("sound_time"):
            try:
                sound_instant = _timestamp(record["sound_time"])
            except ValueError as error:
                raise ValueError(f"{context}的声级计读数时刻无效。{error}。") from None
            record["sound_time"] = sound_instant.isoformat(sep=" ")
        else:
            record["sound_time"] = record["timestamp"]
        if record["sound_time"] in sound_seen:
            raise ValueError(f"{context}的声级计读数时刻 {record['sound_time']} 重复，请核对声级计导出。")
        sound_seen.add(record["sound_time"])
        row_equipment = (record["platform"], record["fan_id"])
        if equipment is None:
            equipment = row_equipment
        if row_equipment != equipment:
            raise ValueError(f"{context}与首条记录的站台或风机不同，请按站台和风机拆分数据。")
        readings.append(record)
    return sorted(readings, key=lambda record: record["timestamp"])


def _run_parameters(params):
    if not isinstance(params, dict):
        raise ValueError("滞后扫描参数格式无效。")
    values = {}
    for field in SPEC["params"]:
        key = field["key"]
        value = params.get(key, field["default"])
        if type(value) is not int or not field["min"] <= value <= field["max"]:
            raise ValueError(f"「{field['label']}」须为 {field['min']} 至 {field['max']} 的整数。")
        values[key] = value
    return values


def _fan_grid(readings, sample_seconds):
    """风机时间戳构成基准采样网格；离网格或重复时刻在导入校验阶段已拦截。"""
    start = datetime.fromisoformat(readings[0]["timestamp"])
    grid = {}
    last_slot = 0
    for record in readings:
        timestamp = datetime.fromisoformat(record["timestamp"])
        offset = timestamp - start
        slot, remainder = divmod(offset.days * 86400 + offset.seconds, sample_seconds)
        if remainder:
            raise ValueError(f"{record['timestamp']} 未落在 {sample_seconds} 秒网格上，请核对采样间隔和两路时钟。")
        grid[slot] = record
        last_slot = slot
    return start, grid, last_slot + 1


def _sound_series(sound_readings, weights, scenario, sample_seconds, span, grid_start):
    """单个漂移场景下把声级读数就近落到风机网格。

    返回 (声级格字典, 落格明细)。明细逐条给出该场景下的格号或缺测原因；
    网格外、撞格（多条读数争同一格）的声级按缺测。
    """
    placed = {}
    collisions = set()
    details = []
    for record, weight in zip(sound_readings, weights):
        offset = clock.corrected_offset_seconds(
            {**record, "_grid_start": grid_start.isoformat(sep=" ")}, weight, scenario)
        slot = clock.snap_slot(offset, sample_seconds, span)
        reason = None
        if slot is None:
            reason = "校正后落在风机网格外"
        elif slot in placed:
            collisions.add(slot)
            reason = "与其他声级读数撞格"
        if reason is None and slot not in collisions:
            placed[slot] = record["sound_dba"]
        details.append({"sound_time": record["sound_time"], "offset_seconds": round(offset, 3),
                        "slot": slot, "reason": reason})
    # 撞格涉及的先到读数也要撤出。
    for slot in collisions:
        placed.pop(slot, None)
    for item in details:
        if item["slot"] in collisions and item["reason"] is None:
            item["reason"] = "与其他声级读数撞格"
    # 空白读数本来就是缺测，不进入声级序列但保留落格位置信息。
    series = {slot: value for slot, value in placed.items() if value is not None}
    return series, details


def _missing_periods(observed, span, start, sample_seconds, channel):
    periods = []
    previous = -1
    for slot in (*observed, span):
        missing = slot - previous - 1
        if missing:
            first = start + timedelta(seconds=(previous + 1) * sample_seconds)
            last = start + timedelta(seconds=(slot - 1) * sample_seconds)
            periods.append({
                "channel": channel,
                "start": first.isoformat(sep=" "),
                "end": last.isoformat(sep=" "),
                "missing_slots": missing,
            })
        previous = slot
    return periods


def _pearson(pairs):
    centered = []
    for values in zip(*pairs):
        # Shift the origin before centering to keep constant readings exactly constant.
        shifted = [value - values[0] for value in values]
        mean = fmean(shifted)
        deviations = [value - mean for value in shifted]
        norm = hypot(*deviations)
        if not norm:
            return None
        centered.append([value / norm for value in deviations])
    cross = fsum(left * right for left, right in zip(*centered))
    return max(-1.0, min(1.0, cross))


def _lag_row(lag, fan, sound, span, sample_seconds, min_pairs):
    pairs = [(value, sound[slot + lag]) for slot, value in fan.items() if slot + lag in sound]
    count = len(pairs)
    possible = max(span - abs(lag), 0)
    coefficient = _pearson(pairs) if count >= min_pairs else None
    if not possible:
        status = "时间窗无重叠"
    elif count < min_pairs:
        status = "配对不足"
    elif coefficient is None:
        status = "配对内序列恒定"
    else:
        status = "已计算"
    return {
        "lag_samples": lag,
        "lag_seconds": lag * sample_seconds,
        "pair_count": count,
        "possible_pairs": possible,
        "missing_pairs": possible - count,
        "coverage": round(count / possible, 6) if possible else None,
        "pearson_r": coefficient,
        "status": status,
    }


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def _interpret(peak, max_lag, possible_peaks, no_peak_rows, retest, interval, scenario_count):
    if not possible_peaks:
        reasons = "、".join(f"{row['reason']}（{row['scenario_count']} 个场景）" for row in no_peak_rows) or "无有效配对"
        return (
            f"全部 {scenario_count} 个校时场景都没有可判读的相关峰档（{reasons}）。",
            "先补齐缺测或选取风机发生调频的时段，并收窄对时偏差区间，再用相同口径复核。",
        )
    peak_text = "、".join(f"{item['lag_seconds']:+d} 秒（{item['scenario_count']} 个场景）" for item in possible_peaks)
    if retest:
        only = possible_peaks[0]
        headline = (
            f"{scenario_count} 个校时场景的候选峰一致落在 {only['lag_seconds']:+d} 秒峰档，建议安排现场复测。"
        )
    else:
        tail = f"；另有 {sum(row['scenario_count'] for row in no_peak_rows)} 个场景无峰" if no_peak_rows else ""
        headline = f"各校时场景未落在同一峰档，可能峰档：{peak_text}{tail}，暂不建议复测。"
    if peak is None:
        return headline, "基准场景无可计算档位，请先补齐缺测或收窄对时偏差区间。"
    delay = peak["lag_seconds"]
    relation = "同向" if peak["pearson_r"] > 0 else "反向" if peak["pearson_r"] < 0 else "无明显线性方向"
    if delay > 0:
        timing = f"基准场景候选峰显示声级变化晚于风机 {delay} 秒"
        action = "按这一时间差复看调频指令、风阀动作及列车进出站记录。"
    elif delay < 0:
        timing = f"基准场景候选峰显示声级变化早于风机 {-delay} 秒"
        action = "先核对声级计和楼控时钟，再检查列车、客流或其他设备的时间记录。"
    else:
        timing = "基准场景候选峰位于零滞后"
        action = "同一采样格的配对达到候选峰，复测时可缩短采样间隔以检查先后。"
    if max_lag and abs(peak["lag_samples"]) == max_lag:
        action += " 当前峰位于扫描边界，可用更长的连续窗口和更宽的滞后范围复核。"
    if abs(peak["pearson_r"]) < 0.5:
        action += " 该档线性关联较弱，先在无列车通过的连续窗口复测。"
    if not retest:
        action += f" 可能峰档（{peak_text}）随对时偏差漂移，复测前先核对首尾对时记录。"
    return f"{headline} {timing}，{relation}，基准场景 r={peak['pearson_r']:.4f}（{peak['pair_count']} 对）。", action


def _no_peak_reasons(lag_rows, min_pairs):
    """汇总一个场景下没有候选峰的原因。"""
    reasons = []
    if not any(row["pearson_r"] is not None for row in lag_rows):
        if all(row["status"] == "时间窗无重叠" for row in lag_rows):
            reasons.append("时间窗无重叠")
        elif not any(row["pair_count"] >= min_pairs for row in lag_rows):
            reasons.append("所有档位配对不足")
        else:
            reasons.append("达配对数的档位序列恒定，无法计算 r")
    return reasons


def _peak_pick(lag_rows):
    valid = [row for row in lag_rows if row["pearson_r"] is not None]
    return max(valid, key=lambda row: (
        round(abs(row["pearson_r"]), 12), row["pair_count"], -abs(row["lag_samples"]), -row["lag_samples"],
    ), default=None)


def _scenario_analysis(fan, sound, span, sample_seconds, min_pairs, max_lag):
    lag_rows = [_lag_row(lag, fan, sound, span, sample_seconds, min_pairs)
                for lag in range(-max_lag, max_lag + 1)]
    peak = _peak_pick(lag_rows)
    return lag_rows, peak, _no_peak_reasons(lag_rows, min_pairs)


def analyze(rows, normalized_params, clock_sync=None):
    """运行滞后扫描。

    clock_sync 为排队时快照的首尾对时记录（含偏差区间）。缺省时按零偏差单场景处理，
    声级读数时刻直接落在与时间戳相同的网格位置，保持旧口径。
    """
    readings = validate_rows(rows)
    options = _run_parameters(normalized_params)
    interval, max_lag, min_pairs = (options[key] for key in ("sample_seconds", "max_lag", "min_pairs"))
    start, grid, span = _fan_grid(readings, interval)
    fan = {slot: record["fan_hz"] for slot, record in grid.items() if record["fan_hz"] is not None}
    sound_readings = [record for record in readings if record["sound_dba"] is not None]

    if clock_sync:
        sync_pair = [clock.validate_clock_sync(item) for item in clock_sync]
        sync_pair.sort(key=lambda item: clock.ENDPOINTS.index(item["endpoint"]))
        if [item["endpoint"] for item in sync_pair] != list(clock.ENDPOINTS):
            raise ValueError("对时记录须包含首尾两次对时，请先在对时面板补齐。")
        clock.validate_pair(*sync_pair)
        scenarios = clock.drift_scenarios(sync_pair, interval)
        weights = clock.drift_weights(readings, sync_pair)
        sound_weights = [weight for record, weight in zip(readings, weights)
                         if record["sound_dba"] is not None]
        clock_note = (
            f"首尾对时偏差区间：首 {sync_pair[0]['deviation_low']:g}~{sync_pair[0]['deviation_high']:g} 秒，"
            f"末 {sync_pair[1]['deviation_low']:g}~{sync_pair[1]['deviation_high']:g} 秒，"
            f"中间匀速漂移，共枚举 {len(scenarios)} 个漂移场景。"
        )
    else:
        sync_pair = None
        scenarios = [(0.0, 0.0)]
        sound_weights = None
        clock_note = "未提供对时记录，按声级计与基准时钟零偏差处理。"

    scenario_results = []
    peak_lag_counts = {}
    no_peak_reason_counts = {}
    base_lags = None
    base_peak = None
    mapping_details = None
    for index, scenario in enumerate(scenarios):
        if sync_pair is None:
            # 零偏差：声级按时间戳直接落格，等价于旧版同格对齐口径。
            sound = {slot: record["sound_dba"] for slot, record in grid.items()
                     if record["sound_dba"] is not None}
            details = [{"sound_time": record["sound_time"],
                        "offset_seconds": slot * interval, "slot": slot, "reason": None}
                       for slot, record in grid.items()]
        else:
            sound, details = _sound_series(sound_readings, sound_weights, scenario,
                                           interval, span, start)
        lag_rows, peak, reasons = _scenario_analysis(fan, sound, span, interval, min_pairs, max_lag)
        if index == 0:
            base_lags, base_peak, mapping_details = lag_rows, peak, details
        if peak is not None:
            key = peak["lag_samples"]
            peak_lag_counts[key] = peak_lag_counts.get(key, 0) + 1
        else:
            for reason in reasons:
                no_peak_reason_counts[reason] = no_peak_reason_counts.get(reason, 0) + 1
        scenario_results.append({
            "start_deviation": scenario[0], "end_deviation": scenario[1],
            "peak_lag_samples": peak["lag_samples"] if peak else None,
            "peak_lag_seconds": peak["lag_seconds"] if peak else None,
            "pearson_r": round(peak["pearson_r"], 6) if peak else None,
            "pair_count": peak["pair_count"] if peak else None,
            "sound_slots": len(sound),
            "no_peak_reasons": reasons,
        })

    lags = base_lags
    peak = base_peak
    total_scenarios = len(scenarios)
    consensus_lag = next(iter(peak_lag_counts)) if len(peak_lag_counts) == 1 and not no_peak_reason_counts else None
    possible_peaks = sorted(
        ({"lag_samples": lag, "lag_seconds": lag * interval,
          "scenario_count": count, "scenario_share": round(count / total_scenarios, 4)}
         for lag, count in peak_lag_counts.items()),
        key=lambda item: (-item["scenario_count"], abs(item["lag_samples"]), item["lag_samples"]),
    )
    no_peak_rows = [{"reason": reason, "scenario_count": count,
                     "scenario_share": round(count / total_scenarios, 4)}
                    for reason, count in sorted(no_peak_reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    retest = consensus_lag is not None
    headline, action = _interpret(peak, max_lag, possible_peaks, no_peak_rows, retest, interval, total_scenarios)

    gaps = _missing_periods(grid, span, start, interval, "整行未采集")
    gaps.extend(_missing_periods(fan, span, start, interval, _CHANNELS[0][1]))
    # 基准场景下声级落格缺测（网格外/撞格）。
    if sync_pair is not None:
        missing_sound = [
            item for item in mapping_details
            if item["reason"] in ("校正后落在风机网格外", "与其他声级读数撞格")
        ]
        for item in missing_sound:
            gaps.append({"channel": f"站台声级（{item['reason']}）", "start": item["sound_time"],
                         "end": item["sound_time"], "missing_slots": 1})
    gaps.sort(key=lambda gap: (gap["start"], gap["channel"]))
    first, last = readings[0], readings[-1]
    zero = lags[max_lag]
    return {
        "state": "warning" if gaps or len([r for r in lags if r["pearson_r"] is not None]) < len(lags) \
            or not retest else "ok",
        "headline": headline,
        "note": (
            f"{first['platform']} · {first['fan_id']}，{first['timestamp']} 至 {last['timestamp']}，每 {interval} 秒一格。"
            f"风机在 t，声级在 t + 滞后。每档至少 {min_pairs} 对，仅使用两路均有读数的配对。{clock_note}"
        ),
        "retest_recommended": retest,
        "metrics": [
            {"label": "漂移场景数", "value": total_scenarios,
             "detail": "首尾偏差区间枚举的匀速漂移组合"},
            {"label": "复测建议", "value": "建议复测" if retest else "暂不建议复测",
             "detail": (f"全部场景峰档一致（{consensus_lag * interval:+d} 秒）" if retest
                        else "各校时场景峰档不一致或存在无峰场景")},
            {"label": "可能峰档数", "value": len(possible_peaks),
             "detail": "、".join(f"{item['lag_seconds']:+d} 秒×{item['scenario_count']} 场景"
                                 for item in possible_peaks) or "无任何可判读峰档"},
            {"label": "候选滞后（秒，基准场景）", "value": peak["lag_seconds"] if peak else None,
             "detail": "正值表示声级晚于风机；基准场景取偏差区间下限组合"},
            {"label": "候选峰 Pearson r（基准场景）", "value": round(peak["pearson_r"], 6) if peak else None,
             "detail": f"{peak['pair_count']}/{peak['possible_pairs']} 对" if peak else "配对不足或配对内序列恒定"},
            {"label": "零滞后有效配对", "value": zero["pair_count"], "detail": f"理论可配 {span} 对"},
            {"label": "预期采样格", "value": span, "detail": f"实到 {len(grid)} 行，整行未采集 {span - len(grid)} 格"},
            {"label": "风机缺测格", "value": span - len(fan), "detail": "含整行未采集"},
            {"label": "声级落格缺测条数（基准场景）",
             "value": len([item for item in (mapping_details or []) if item["reason"]]),
             "detail": "网格外或撞格；空白读数另计为缺测格"},
        ],
        "tables": [
            _table("漂移场景峰档汇总", (
                ("start_deviation", "首端偏差（秒）"), ("end_deviation", "末端偏差（秒）"),
                ("peak_lag_seconds", "峰档滞后（秒）"), ("pearson_r", "Pearson r"),
                ("pair_count", "峰档配对数"), ("sound_slots", "声级落格数"),
                ("no_peak_reasons", "无峰原因"),
            ), [{**item, "no_peak_reasons": "、".join(item["no_peak_reasons"])} for item in scenario_results]),
            _table("可能峰档（按支持场景数排序）", (
                ("lag_seconds", "滞后（秒）"), ("lag_samples", "滞后（格）"),
                ("scenario_count", "支持场景数"), ("scenario_share", "场景占比（0–1）"),
            ), possible_peaks),
            _table("无峰原因", (
                ("reason", "原因"), ("scenario_count", "涉及场景数"), ("scenario_share", "场景占比（0–1）"),
            ), no_peak_rows),
            _table("滞后扫描明细（基准场景）", (
                ("lag_samples", "滞后（格）"), ("lag_seconds", "滞后（秒）"), ("pearson_r", "Pearson r"),
                ("pair_count", "有效配对"), ("possible_pairs", "理论配对"), ("missing_pairs", "缺失配对"),
                ("coverage", "配对覆盖率（0–1）"), ("status", "计算状态"),
            ), lags),
            _table("声级落格明细（基准场景）", (
                ("sound_time", "声级计读数时刻"), ("offset_seconds", "校正后相对网格起点（秒）"),
                ("slot", "落格（格号）"), ("reason", "缺测原因"),
            ), mapping_details or []),
            _table("缺测时段（首末采样时刻均含）", (
                ("channel", "采集通道"), ("start", "首个缺测时刻"), ("end", "末个缺测时刻"),
                ("missing_slots", "缺测格数"),
            ), gaps),
        ],
        "charts": [
            {"title": "可能峰档的支持场景数", "kind": "bar", "series": [
                {"label": f"{item['lag_seconds']:+d} 秒", "value": item["scenario_count"]}
                for item in possible_peaks]},
            {"title": "滞后与 Pearson r（基准场景，仅可计算档位）", "kind": "bar", "series": [
                {"label": f"{row['lag_seconds']:+d} 秒", "value": row["pearson_r"]}
                for row in lags if row["pearson_r"] is not None]},
            {"title": "各滞后有效配对数（基准场景）", "kind": "bar", "series": [
                {"label": f"{row['lag_seconds']:+d} 秒", "value": row["pair_count"]} for row in lags]},
        ],
        "sections": [
            {"title": "复测判定", "text": (
                f"全部 {total_scenarios} 个校时场景的候选峰都落在 {consensus_lag * interval:+d} 秒同一峰档，"
                "可据此安排现场复测，并在复测时同步核对两台时钟。"
                if retest else
                "校时偏差区间内各漂移场景的峰档不一致或存在无峰场景，暂不建议复测；"
                "请先收窄对时偏差区间或补齐缺测，再重跑核对。")},
            {"title": "现场复核", "text": f"{action} 相关峰用于缩小排查时段，归因仍需现场复测。"},
            {"title": "缺测与交班", "text": (
                "风机和声级的缺测时段各自包含整行未采集的时间格，声级落格缺测另见落格明细。"
                "核对缺测表并回补原始记录后，沿用采样间隔重跑。"
                if gaps else "观测窗口内两路数据完整。交班时记录所用采样间隔、对时偏差区间、滞后和配对数。"
            )},
        ],
        "clock_sync": sync_pair,
    }
