"""澄湾站调频巡检样本，含已知三格滞后和三类采集缺口。"""

from datetime import datetime, timedelta
from random import Random


def demo_rows() -> list[dict]:
    rng = Random(52)
    start = datetime(2026, 11, 21, 9, 30)
    sweep = (31, 44, 36, 49, 39, 28, 46, 34, 42)
    frequency = [sweep[slot // 10] + rng.uniform(-1.2, 1.2) for slot in range(90)]
    rows = []
    for slot, fan_hz in enumerate(frequency):
        sound = 46 + 0.57 * frequency[max(0, slot - 3)] + rng.uniform(-0.7, 0.7)
        if 67 <= slot <= 69:
            continue
        rows.append({
            "platform": "澄湾站下行站台",
            "fan_id": "TVF-02",
            "timestamp": (start + timedelta(seconds=10 * slot)).isoformat(sep=" "),
            "sound_dba": None if 54 <= slot <= 57 else round(sound, 2),
            "fan_hz": None if 32 <= slot <= 34 else round(fan_hz, 2),
        })
    return rows
