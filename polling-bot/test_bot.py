"""
Tests that run WITHOUT any network, Telegram token or Cloudflare key.
    python -m pytest test_bot.py -v
    or: python test_bot.py

They check the logic that would otherwise silently eat your free quota:
neuron math, per-user caps, input resize (<512 for klein-4b), and
base64 response decoding.
"""

import base64
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

import cloudflare
from bot import resize_input_for_klein
from usage import UsageStore, DAILY_NEURON_BUDGET


class TestNeuronMath(unittest.TestCase):
    def test_schnell_1024(self):
        # 4 tiles * 4.80 + 4 steps * 9.60 = 57.6
        self.assertAlmostEqual(cloudflare.NEURONS_SCHNELL_1024, 57.6)
        self.assertAlmostEqual(10_000 / 57.6, 173.6, places=1)

    def test_klein_edit_1024(self):
        # 4 output tiles * 26.05 + 1 input tile * 5.37 = 109.57
        self.assertAlmostEqual(cloudflare.NEURONS_KLEIN_EDIT_1024, 109.6, places=1)
        self.assertAlmostEqual(10_000 / 109.6, 91.2, places=1)

    def test_budget_constant(self):
        self.assertEqual(DAILY_NEURON_BUDGET, 10_000.0)


class TestCaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.store = UsageStore(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_txt_cap_blocks_6th(self):
        for i in range(5):
            ok, _, used, _ = self.store.check_and_count(1, "txt", 5, 3, 57.6)
            self.assertTrue(ok)
            self.assertEqual(used, i + 1)
        ok, reason, _, _ = self.store.check_and_count(1, "txt", 5, 3, 57.6)
        self.assertFalse(ok)
        self.assertIn("5", reason)

    def test_edit_cap_separate_from_txt(self):
        for _ in range(3):
            ok, _, _, _ = self.store.check_and_count(2, "edit", 5, 3, 109.6)
            self.assertTrue(ok)
        ok, _, _, _ = self.store.check_and_count(2, "edit", 5, 3, 109.6)
        self.assertFalse(ok)
        # txt still allowed - caps are separate
        ok, _, _, _ = self.store.check_and_count(2, "txt", 5, 3, 57.6)
        self.assertTrue(ok)

    def test_global_neuron_cap_blocks(self):
        # Simulate a day almost used up: add 9,950 neurons, then a
        # 57.6-neuron picture should be refused.
        self.store.add_neurons(9950.0)
        ok, reason, _, _ = self.store.check_and_count(3, "txt", 5, 3, 57.6)
        self.assertFalse(ok)
        self.assertIn("quota", reason.lower())

    def test_status(self):
        self.store.check_and_count(4, "txt", 5, 3, 57.6)
        self.store.add_neurons(57.6)
        s = self.store.status(4, 5, 3)
        self.assertEqual(s["txt_used"], 1)
        self.assertEqual(s["neurons_used"], 57.6)
        self.assertAlmostEqual(s["neurons_left"], 10000 - 57.6)


class TestResize(unittest.TestCase):
    def test_input_is_smaller_than_512(self):
        from PIL import Image
        img = Image.new("RGB", (2000, 1500), "red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        out = resize_input_for_klein(buf.getvalue())
        result = Image.open(io.BytesIO(out))
        self.assertLess(max(result.size), 512)
        self.assertEqual(result.size, (500, 375))

    def test_small_image_unchanged_size(self):
        from PIL import Image
        img = Image.new("RGB", (400, 300), "blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        out = resize_input_for_klein(buf.getvalue())
        result = Image.open(io.BytesIO(out))
        self.assertEqual(result.size, (400, 300))


class TestDecode(unittest.TestCase):
    def test_decode_base64_json(self):
        payload = base64.b64encode(b"fake-image-bytes").decode()
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"success": True, "result": {"image": payload}}
        self.assertEqual(cloudflare._decode_result(resp), b"fake-image-bytes")

    def test_decode_raw_image(self):
        resp = MagicMock()
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.content = b"raw-bytes"
        self.assertEqual(cloudflare._decode_result(resp), b"raw-bytes")

    def test_decode_error_raises(self):
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"success": False, "errors": [{"message": "bad prompt"}]}
        with self.assertRaises(RuntimeError):
            cloudflare._decode_result(resp)


if __name__ == "__main__":
    unittest.main()
