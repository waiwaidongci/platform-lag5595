"""固定采样网格上的配对 Pearson 互相关及缺测排查。"""

from datetime import datetime, timedelta
from math import fsum, hypot
from statistics import fmean

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
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
                    raise ValueError(f"{context}的「{label}」须为 1 至 300 个字符。")
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


def _sample_grid(readings, sample_seconds):
    start = datetime.fromisoformat(readings[0]["timestamp"])
    grid = {}
    for record in readings:
        timestamp = datetime.fromisoformat(record["timestamp"])
        offset = timestamp - start
        slot, remainder = divmod(offset.days * 86400 + offset.seconds, sample_seconds)
        if remainder:
            raise ValueError(f"{record['timestamp']} 未落在 {sample_seconds} 秒网格上，请核对采样间隔和两路时钟。")
        grid[slot] = record
    return start, grid, slot + 1


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


def _interpret(peak, max_lag):
    if peak is None:
        return "没有可判读的相关档位。", "先补齐缺测或选取风机发生调频的时段，再用相同口径复核。"
    delay = peak["lag_seconds"]
    relation = "同向" if peak["pearson_r"] > 0 else "反向" if peak["pearson_r"] < 0 else "无明显线性方向"
    if delay > 0:
        timing = f"候选峰显示声级变化晚于风机 {delay} 秒"
        action = "按这一时间差复看调频指令、风阀动作及列车进出站记录。"
    elif delay < 0:
        timing = f"候选峰显示声级变化早于风机 {-delay} 秒"
        action = "先核对声级计和楼控时钟，再检查列车、客流或其他设备的时间记录。"
    else:
        timing = "候选峰位于零滞后"
        action = "同一采样格的配对达到候选峰，复测时可缩短采样间隔以检查先后。"
    if max_lag and abs(peak["lag_samples"]) == max_lag:
        action += " 当前峰位于扫描边界，可用更长的连续窗口和更宽的滞后范围复核。"
    if abs(peak["pearson_r"]) < 0.5:
        action += " 该档线性关联较弱，先在无列车通过的连续窗口复测。"
    return (
        f"{timing}，{relation}，r={peak['pearson_r']:.4f}（{peak['pair_count']} 对）。",
        action,
    )


def analyze(rows, normalized_params):
    readings = validate_rows(rows)
    options = _run_parameters(normalized_params)
    interval, max_lag, min_pairs = (options[key] for key in ("sample_seconds", "max_lag", "min_pairs"))
    start, grid, span = _sample_grid(readings, interval)
    fan, sound = (
        {slot: record[key] for slot, record in grid.items() if record[key] is not None}
        for key, _ in _CHANNELS
    )
    lags = [_lag_row(lag, fan, sound, span, interval, min_pairs) for lag in range(-max_lag, max_lag + 1)]
    valid = [row for row in lags if row["pearson_r"] is not None]
    peak = max(valid, key=lambda row: (
        round(abs(row["pearson_r"]), 12), row["pair_count"], -abs(row["lag_samples"]), -row["lag_samples"],
    ), default=None)
    headline, action = _interpret(peak, max_lag)
    gaps = _missing_periods(grid, span, start, interval, "整行未采集")
    for series, (_, label) in zip((fan, sound), _CHANNELS):
        gaps.extend(_missing_periods(series, span, start, interval, label))
    gaps.sort(key=lambda gap: (gap["start"], gap["channel"]))
    first, last = readings[0], readings[-1]
    zero = lags[max_lag]
    return {
        "state": "warning" if gaps or len(valid) < len(lags) else "ok",
        "headline": headline,
        "note": (
            f"{first['platform']} · {first['fan_id']}，{first['timestamp']} 至 {last['timestamp']}，每 {interval} 秒一格。"
            f"风机在 t，声级在 t + 滞后。每档至少 {min_pairs} 对，仅使用两路均有读数的配对。"
        ),
        "metrics": [
            {"label": "候选滞后（秒）", "value": peak["lag_seconds"] if peak else None,
             "detail": "正值表示声级晚于风机"},
            {"label": "候选峰 Pearson r", "value": round(peak["pearson_r"], 6) if peak else None,
             "detail": f"{peak['pair_count']}/{peak['possible_pairs']} 对" if peak else "配对不足或配对内序列恒定"},
            {"label": "零滞后有效配对", "value": zero["pair_count"], "detail": f"理论可配 {span} 对"},
            {"label": "预期采样格", "value": span, "detail": f"实到 {len(grid)} 行，整行未采集 {span - len(grid)} 格"},
            {"label": "风机缺测格", "value": span - len(fan), "detail": "含整行未采集"},
            {"label": "声级缺测格", "value": span - len(sound), "detail": "含整行未采集"},
        ],
        "tables": [
            _table("滞后扫描明细", (
                ("lag_samples", "滞后（格）"), ("lag_seconds", "滞后（秒）"), ("pearson_r", "Pearson r"),
                ("pair_count", "有效配对"), ("possible_pairs", "理论配对"), ("missing_pairs", "缺失配对"),
                ("coverage", "配对覆盖率（0–1）"), ("status", "计算状态"),
            ), lags),
            _table("缺测时段（首末采样时刻均含）", (
                ("channel", "采集通道"), ("start", "首个缺测时刻"), ("end", "末个缺测时刻"),
                ("missing_slots", "缺测格数"),
            ), gaps),
        ],
        "charts": [
            {"title": "滞后与 Pearson r（仅可计算档位）", "kind": "bar", "series": [
                {"label": f"{row['lag_seconds']:+d} 秒", "value": row["pearson_r"]} for row in valid
            ]},
            {"title": "各滞后有效配对数", "kind": "bar", "series": [
                {"label": f"{row['lag_seconds']:+d} 秒", "value": row["pair_count"]} for row in lags
            ]},
        ],
        "sections": [
            {"title": "现场复核", "text": f"{action} 相关峰用于缩小排查时段，归因仍需现场复测。"},
            {"title": "缺测与交班", "text": (
                "风机和声级的缺测时段各自包含整行未采集的时间格。核对缺测表并回补原始记录后，沿用采样间隔重跑。"
                if gaps else "观测窗口内两路数据完整。交班时记录所用采样间隔、滞后和配对数。"
            )},
        ],
    }
