"""声级计对时记录、匀速漂移区间与就近落格。

口径：
- 对时记录给出声级计时钟相对基准时钟的偏差（秒）：偏差 = 声级计显示时刻 − 基准时刻，
  真实时刻 = 声级计读数时刻 − 偏差。
- 导入批次首尾各有一次对时，每次对时只确定一个偏差区间 [偏差下限, 偏差上限]，
  区间内任意起止偏差组合都可能成立；两次对时之间按匀速（线性）漂移。
- 声级读数校正后就近落到风机采样格，恰在半格上往后（格号更大）取；
  落在网格跨度之外、或同一格被多条读数撞格，则受影响读数按缺测处理。
"""

from datetime import datetime, timedelta

# 单次对时偏差允许范围（秒），覆盖现场可能的钟差量级。
MAX_DEVIATION_SECONDS = 600
# 每条对时记录偏差区间的最大宽度，避免枚举场景失控。
MAX_SPAN_SECONDS = 3600
# 枚举漂移场景数量的硬上限，超出时要求先收窄偏差区间。
MAX_SCENARIOS = 120

ENDPOINTS = ("start", "end")
_ENDPOINT_LABELS = {"start": "首次对时", "end": "末次对时"}


def parse_instant(value, label="对时时刻"):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}须填写本地时间 YYYY-MM-DD HH:MM:SS") from None
    if result.isoformat(sep=" ") != value.replace("T", " ") or result.tzinfo is not None:
        raise ValueError(f"{label}须填写本地时间 YYYY-MM-DD HH:MM:SS，精确到整秒")
    return result


def _check_deviation(value, label):
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"{label}须为秒数数值。")
    if not -MAX_DEVIATION_SECONDS <= value <= MAX_DEVIATION_SECONDS:
        raise ValueError(f"{label}须在 ±{MAX_DEVIATION_SECONDS} 秒之间。")
    return float(value)


def validate_clock_sync(payload):
    """校验单条对时记录的可序列化字段，endpoint 取 start/end。"""
    if not isinstance(payload, dict):
        raise ValueError("对时记录格式无效。")
    endpoint = payload.get("endpoint")
    if endpoint not in ENDPOINTS:
        raise ValueError("对时端别须为 start（首次）或 end（末次）。")
    label = _ENDPOINT_LABELS[endpoint]
    observed_at = parse_instant(payload.get("observed_at"), f"{label}的观测时刻")
    low = _check_deviation(payload.get("deviation_low"), f"{label}的偏差下限")
    high = _check_deviation(payload.get("deviation_high"), f"{label}的偏差上限")
    if low > high:
        raise ValueError(f"{label}的偏差下限不能大于上限。")
    if high - low > MAX_SPAN_SECONDS:
        raise ValueError(f"{label}的偏差区间宽度不能超过 {MAX_SPAN_SECONDS} 秒，请先收窄。")
    note = payload.get("note", "")
    if note is not None and not isinstance(note, str) or isinstance(note, str) and len(note) > 300:
        raise ValueError(f"{label}备注须为 300 字以内文本。")
    return {
        "endpoint": endpoint,
        "observed_at": observed_at.isoformat(sep=" "),
        "deviation_low": low,
        "deviation_high": high,
        "note": (note or "").strip(),
    }


def validate_pair(start_sync, end_sync):
    """两条记录联合校验：末次观测时刻必须晚于首次。"""
    start_at = parse_instant(start_sync["observed_at"], "首次对时的观测时刻")
    end_at = parse_instant(end_sync["observed_at"], "末次对时的观测时刻")
    if end_at <= start_at:
        raise ValueError("末次对时时刻必须晚于首次对时时刻。")


def _half_grid_values(low, high, half):
    """闭区间 [low, high] 内半格整数倍的值，并包含两个端点。"""
    values = {low, high}
    first = -(-low // half) * half if low <= 0 else (low // half + 1) * half
    current = first
    while current <= high:
        values.add(round(float(current), 9))
        current += half
    return sorted(values)


def drift_scenarios(sync_pair, sample_seconds):
    """枚举需要评估的漂移场景。

    每个场景为 (首端偏差, 末端偏差)。除偏差矩形的半格规则网格外，
    额外补入落在各边范围内的半格分界点，使每条读数的落格翻转边界两侧都被评估。
    """
    start_sync, end_sync = sync_pair
    half = sample_seconds / 2
    start_values = _half_grid_values(start_sync["deviation_low"], start_sync["deviation_high"], half)
    end_values = _half_grid_values(end_sync["deviation_low"], end_sync["deviation_high"], half)
    scenarios = {(round(a, 9), round(b, 9)) for a in start_values for b in end_values}
    if len(scenarios) > MAX_SCENARIOS:
        raise ValueError(
            f"偏差区间组合产生 {len(scenarios)} 个漂移场景，超过 {MAX_SCENARIOS} 个上限，"
            "请收窄首尾对时的偏差区间后再运行。"
        )
    return sorted(scenarios)


def drift_weights(readings, sync_pair):
    """按读数的声级计时刻计算它在两次对时之间的线性权重（0→1，区间外截断）。"""
    start_at = parse_instant(sync_pair[0]["observed_at"])
    end_at = parse_instant(sync_pair[1]["observed_at"])
    width = (end_at - start_at).total_seconds()
    weights = []
    for record in readings:
        instant = parse_instant(record["sound_time"])
        progress = (instant - start_at).total_seconds() / width
        weights.append(min(1.0, max(0.0, progress)))
    return weights


def corrected_offset_seconds(record, weight, scenario):
    """给定漂移场景下，读数校正时刻相对风机网格起点的秒数。"""
    start_dev, end_dev = scenario
    deviation = start_dev + (end_dev - start_dev) * weight
    meter_time = parse_instant(record["sound_time"])
    grid_start = parse_instant(record["_grid_start"])
    return (meter_time - grid_start).total_seconds() - deviation


def snap_slot(offset_seconds, sample_seconds, span):
    """就近落格，半格往后取；返回格号，网格外返回 None。"""
    half = sample_seconds / 2
    quotient, remainder = divmod(offset_seconds, sample_seconds)
    slot = int(quotient) + (1 if remainder >= half else 0)
    if slot < 0 or slot >= span:
        return None
    return slot
