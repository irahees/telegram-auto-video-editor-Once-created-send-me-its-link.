import unittest

from bot import remap_shots_to_beats


class TimelineTests(unittest.TestCase):
    def test_empty_beats_preserve_timeline(self):
        timeline = {"shots": [{"start": 0, "end": 1, "duration": 1}]}
        self.assertEqual(remap_shots_to_beats(timeline, {})["shots"], timeline["shots"])

    def test_beat_snap_preserves_total_duration(self):
        timeline = {
            "shots": [
                {"start": 0, "end": 1.02, "duration": 1.02},
                {"start": 1.02, "end": 2.0, "duration": 0.98},
            ]
        }
        result = remap_shots_to_beats(timeline, {"beats": [0.0, 1.0, 2.0]})
        self.assertAlmostEqual(sum(s["duration"] for s in result["shots"]), 2.0, places=2)
        self.assertEqual(len(result["shots"]), 2)

    def test_short_shots_keep_minimum_spacing(self):
        timeline = {
            "shots": [
                {"duration": 0.5}, {"duration": 0.5}, {"duration": 0.5}
            ]
        }
        result = remap_shots_to_beats(timeline, {"beats": [0.1, 0.2, 0.3, 0.4]})
        starts = [s["start"] for s in result["shots"]]
        self.assertTrue(all(b - a >= 0.44 for a, b in zip(starts, starts[1:])))


if __name__ == "__main__":
    unittest.main()
