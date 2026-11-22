"""对时漂移、就近落格与多场景峰档判定的纯标准库测试。

运行：PYTHONPATH=src python -m unittest discover -s tests -v
"""

import unittest
from datetime import datetime, timedelta

from src.domain import clock
from src.domain.analysis import analyze, validate_rows


def make_sync(low_start, high_start, low_end, high_end,
              start_at="2026-11-21 09:25:00", end_at="2026-11-21 09:50:00"):
    return [
        {"endpoint": "start", "observed_at": start_at,
         "deviation_low": low_start, "deviation_high": high_start, "note": ""},
        {"endpoint": "end", "observed_at": end_at,
         "deviation_low": low_end, "deviation_high": high_end, "note": ""},
    ]


def row(slot, sound=None, fan=None, sound_offset=None, base=datetime(2026, 11, 21, 9, 30)):
    timestamp = (base + timedelta(seconds=10 * slot)).isoformat(sep=" ")
    record = {"platform": "澄湾站下行站台", "fan_id": "TVF-02", "timestamp": timestamp,
              "sound_dba": sound, "fan_hz": fan}
    if sound_offset is not None:
        record["sound_time"] = (base + timedelta(seconds=10 * slot + sound_offset)).isoformat(sep=" ")
    return record


class SnapTests(unittest.TestCase):
    def test_half_grid_rounds_forward(self):
        # 恰在半格上往后取（格号更大）：5 秒落到 1 格，-5 秒落到 0 格。
        self.assertEqual(clock.snap_slot(4.999, 10, 10), 0)
        self.assertEqual(clock.snap_slot(5.0, 10, 10), 1)
        self.assertEqual(clock.snap_slot(-5.0, 10, 10), 0)
        self.assertEqual(clock.snap_slot(0.0, 10, 10), 0)

    def test_outside_grid_is_missing(self):
        self.assertIsNone(clock.snap_slot(-6, 10, 5))
        self.assertIsNone(clock.snap_slot(50, 10, 5))
        # 最后一格（4 号格）的半格上界 45 秒已在网格外；-5.001 的 -1 格同样越界。
        self.assertIsNone(clock.snap_slot(45.0, 10, 5))
        self.assertIsNone(clock.snap_slot(-5.001, 10, 10))

    def test_scenario_enum_includes_half_grid_boundaries(self):
        pair = make_sync(-3, 3, -3, 3)
        scenarios = clock.drift_scenarios(pair, 10)
        # 半格步长 5 秒：-3..3 内补入 0；角点 (-3,-3)、(3,3) 等都在。
        deviations = sorted({value for scenario in scenarios for value in scenario})
        self.assertEqual(deviations, [-3.0, 0.0, 3.0])
        self.assertIn((-3.0, 3.0), scenarios)
        self.assertEqual(len(scenarios), 9)

    def test_scenario_cap(self):
        pair = make_sync(-300, 300, -300, 300)
        with self.assertRaises(ValueError):
            clock.drift_scenarios(pair, 10)

    def test_drift_is_linear_between_endpoints(self):
        pair = make_sync(0, 0, 10, 10)
        readings = [row(0), row(6)]
        clean = [clock.validate_clock_sync(item) for item in pair]
        weights = clock.drift_weights(validate_rows(readings), clean)
        # 09:30 在 09:25→09:50 之间权重 0.2；slot 6 为 09:31，权重 6/25=0.24。
        self.assertAlmostEqual(weights[0], 0.2)
        self.assertAlmostEqual(weights[1], 0.24)
        scenario = (0.0, 10.0)
        offset0 = clock.corrected_offset_seconds(
            {**validate_rows(readings)[0], "_grid_start": readings[0]["timestamp"]}, weights[0], scenario)
        # 偏差 = 0 + 10*0.2 = 2 秒，校正后偏移 -2 秒。
        self.assertAlmostEqual(offset0, -2.0)


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        # 构造声级滞后风机 2 格的强信号数据。
        self.rows = []
        sweep = [31, 44, 36, 49, 39, 28, 46, 34, 42]
        for slot in range(60):
            fan = sweep[slot // 7] + (slot % 3) * 0.3
            sound = 40 + 0.6 * (sweep[max(0, slot - 2) // 7] + (max(0, slot - 2) % 3) * 0.3)
            self.rows.append(row(slot, sound=round(sound, 2), fan=round(fan, 2)))
        self.params = {"sample_seconds": 10, "max_lag": 6, "min_pairs": 20}

    def test_zero_width_sync_keeps_single_scenario_and_finds_peak(self):
        sync = make_sync(0, 0, 0, 0)
        result = analyze(self.rows, self.params, clock_sync=sync)
        self.assertEqual(result["retest_recommended"], True)
        self.assertEqual(result["metrics"][0]["value"], 1)
        self.assertEqual(result["metrics"][1]["value"], "建议复测")
        possible = result["tables"][1]["rows"]
        self.assertEqual(len(possible), 1)
        self.assertEqual(possible[0]["lag_samples"], 2)

    def test_drift_interval_moves_peak_band(self):
        # 偏差区间覆盖一个整格：声级落格随漂移前后移动，峰档在 +1/+2/+3 间变化。
        sync = make_sync(-10, 10, -10, 10)
        result = analyze(self.rows, self.params, clock_sync=sync)
        self.assertEqual(result["retest_recommended"], False)
        possible = result["tables"][1]["rows"]
        lags = {item["lag_samples"] for item in possible}
        self.assertTrue({1, 2, 3} <= lags)
        self.assertIn("暂不建议复测", result["headline"])
        # 场景汇总行数与枚举数一致，且每行给出峰档或无峰原因。
        summary = result["tables"][0]["rows"]
        self.assertEqual(len(summary), result["metrics"][0]["value"])
        self.assertTrue(all(row["peak_lag_seconds"] is not None or row["no_peak_reasons"]
                            for row in summary))

    def test_collision_marks_both_readings_missing(self):
        rows = [row(slot, sound=50.0 + slot, fan=30.0 + slot) for slot in range(5)]
        # slot 1 的读数时刻前移 6 秒：零偏差下校正后位于 4 秒，就近与 0 秒读数同抢 0 格。
        rows[1] = row(1, sound=51.0, fan=31.0, sound_offset=-6)
        sync = make_sync(0, 0, 0, 0)
        result = analyze(rows, {"sample_seconds": 10, "max_lag": 2, "min_pairs": 3},
                         clock_sync=sync)
        mapping = result["tables"][4]
        self.assertEqual(mapping["title"], "声级落格明细（基准场景）")
        collisions = [item for item in mapping["rows"] if item["reason"] == "与其他声级读数撞格"]
        self.assertEqual(len(collisions), 2)
        self.assertEqual({item["slot"] for item in collisions}, {0})

    def test_outside_grid_marks_missing(self):
        rows = [row(0, sound=50.0, fan=30.0),
                row(1, sound=51.0, fan=31.0, sound_offset=-16)]
        sync = make_sync(0, 0, 0, 0)
        result = analyze(rows, {"sample_seconds": 10, "max_lag": 2, "min_pairs": 3},
                         clock_sync=sync)
        mapping = result["tables"][4]["rows"]
        outside = next(item for item in mapping if item["reason"] == "校正后落在风机网格外")
        self.assertEqual(outside["slot"], None)

    def test_missing_pair_reasons_listed_when_no_peak(self):
        # 仅 2 行、min_pairs=10，任何档位都配对不足。
        rows = [row(0, sound=50.0, fan=30.0), row(1, sound=51.0, fan=31.0)]
        sync = make_sync(0, 0, 0, 0)
        result = analyze(rows, {"sample_seconds": 10, "max_lag": 2, "min_pairs": 30},
                         clock_sync=sync)
        self.assertEqual(result["retest_recommended"], False)
        no_peak = result["tables"][2]["rows"]
        self.assertTrue(any("配对不足" in item["reason"] for item in no_peak))
        self.assertEqual(len(result["tables"][1]["rows"]), 0)

    def test_legacy_path_without_clock_sync(self):
        result = analyze(self.rows, self.params)
        self.assertEqual(result["clock_sync"], None)
        self.assertEqual(result["metrics"][0]["value"], 1)
        self.assertEqual(result["tables"][1]["rows"][0]["lag_samples"], 2)

    def test_validate_pair_order(self):
        bad = make_sync(0, 0, 0, 0, start_at="2026-11-21 10:00:00", end_at="2026-11-21 09:00:00")
        with self.assertRaises(ValueError):
            analyze(self.rows, self.params, clock_sync=bad)


if __name__ == "__main__":
    unittest.main()
