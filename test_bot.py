"""
Tests that run WITHOUT any network, Telegram token or Cloudflare key.
    python -m pytest test_bot.py -v
    or: python test_bot.py

They check the logic that would otherwise silently eat your free quota:
neuron math, per-user caps (only delivered pictures count), input
resize (<512 for klein-4b), base64 response decoding, and the
multi-provider failover chain (Cloudflare -> Pollinations -> AI Horde).
"""

import base64
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import aihorde
import cloudflare
import pollinations
import providers
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

    def test_check_does_not_count(self):
        # check() alone must not consume the allowance - only a
        # delivered picture counts (count() after success).
        for _ in range(5):
            ok, _, used, _ = self.store.check(1, "txt", 5, 3)
            self.assertTrue(ok)
            self.assertEqual(used, 0)
        # and count() is what moves it
        used, _ = self.store.count(1, "txt")
        self.assertEqual(used, 1)

    def test_txt_cap_blocks_6th(self):
        for i in range(5):
            ok, _, used, _ = self.store.check(1, "txt", 5, 3)
            self.assertTrue(ok)
            used, _ = self.store.count(1, "txt")
            self.assertEqual(used, i + 1)
        ok, reason, _, _ = self.store.check(1, "txt", 5, 3)
        self.assertFalse(ok)
        self.assertIn("5", reason)

    def test_edit_cap_separate_from_txt(self):
        for _ in range(3):
            ok, _, _, _ = self.store.check(2, "edit", 5, 3)
            self.assertTrue(ok)
            self.store.count(2, "edit")
        ok, _, _, _ = self.store.check(2, "edit", 5, 3)
        self.assertFalse(ok)
        # txt still allowed - caps are separate
        ok, _, _, _ = self.store.check(2, "txt", 5, 3)
        self.assertTrue(ok)

    def test_global_neuron_budget_does_not_block(self):
        # The shared Neuron budget being spent must NOT block a user:
        # the provider chain falls back to Pollinations / AI Horde,
        # which do not use Neurons. The budget is only tracked for /status.
        self.store.add_neurons(9950.0)
        ok, _, _, _ = self.store.check(3, "txt", 5, 3)
        self.assertTrue(ok)

    def test_status(self):
        self.store.check(4, "txt", 5, 3)
        self.store.count(4, "txt")
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


class TestFailoverChain(unittest.TestCase):
    """providers.generate_text_image with mocked engines (no network)."""

    def test_cloudflare_success_short_circuits(self):
        with patch("providers.cloudflare.env_config", return_value=("acct", "tok")), \
             patch("providers.cloudflare.text_to_image",
                   return_value=(b"cf-bytes", 57.6)) as cf, \
             patch("providers.pollinations.text_to_image") as pol, \
             patch("providers.aihorde.text_to_image") as horde:
            out, label, neurons = providers.generate_text_image("a fox")
            self.assertEqual(out, b"cf-bytes")
            self.assertEqual(label, providers.LABEL_CLOUDFLARE)
            self.assertEqual(neurons, 57.6)
            pol.assert_not_called()
            horde.assert_not_called()

    def test_cloudflare_failure_falls_to_pollinations(self):
        with patch("providers.cloudflare.env_config", return_value=("acct", "tok")), \
             patch("providers.cloudflare.text_to_image",
                   side_effect=RuntimeError("Cloudflare auth failed (401/403)")), \
             patch("providers.pollinations.text_to_image",
                   return_value=b"pol-bytes") as pol, \
             patch("providers.aihorde.text_to_image") as horde:
            out, label, neurons = providers.generate_text_image("a fox")
            self.assertEqual(out, b"pol-bytes")
            self.assertEqual(label, providers.LABEL_POLLINATIONS)
            self.assertIsNone(neurons)
            horde.assert_not_called()

    def test_cloudflare_unconfigured_falls_through(self):
        # Missing Cloudflare env must not stop the chain.
        with patch("providers.cloudflare.env_config",
                   side_effect=RuntimeError("Missing CF_ACCOUNT_ID")), \
             patch("providers.pollinations.text_to_image",
                   return_value=b"pol-bytes"):
            out, label, _ = providers.generate_text_image("a fox")
            self.assertEqual(out, b"pol-bytes")
            self.assertEqual(label, providers.LABEL_POLLINATIONS)

    def test_two_failures_fall_to_horde(self):
        with patch("providers.cloudflare.env_config", return_value=("acct", "tok")), \
             patch("providers.cloudflare.text_to_image",
                   side_effect=RuntimeError("429 used up")), \
             patch("providers.pollinations.text_to_image",
                   side_effect=RuntimeError("Pollinations legacy failed (500)")), \
             patch("providers.aihorde.text_to_image",
                   return_value=b"horde-bytes"):
            out, label, neurons = providers.generate_text_image("a fox")
            self.assertEqual(out, b"horde-bytes")
            self.assertEqual(label, providers.LABEL_HORDE)
            self.assertIsNone(neurons)

    def test_all_three_fail_raises_with_each_reason(self):
        with patch("providers.cloudflare.env_config", return_value=("acct", "tok")), \
             patch("providers.cloudflare.text_to_image",
                   side_effect=RuntimeError("cf boom")), \
             patch("providers.pollinations.text_to_image",
                   side_effect=RuntimeError("pol boom")), \
             patch("providers.aihorde.text_to_image",
                   side_effect=RuntimeError("horde boom")):
            with self.assertRaises(RuntimeError) as ctx:
                providers.generate_text_image("a fox")
            msg = str(ctx.exception)
            self.assertIn("Cloudflare", msg)
            self.assertIn("cf boom", msg)
            self.assertIn("Pollinations", msg)
            self.assertIn("pol boom", msg)
            self.assertIn("AI Horde", msg)
            self.assertIn("horde boom", msg)


class TestPollinationsLegacy(unittest.TestCase):
    def test_legacy_no_key_returns_image_bytes(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.content = b"fake-jpeg"
        with patch.dict(os.environ, {}, clear=True), \
             patch("pollinations.requests.get", return_value=resp) as get:
            out = pollinations.text_to_image("a fox")
            self.assertEqual(out, b"fake-jpeg")
            url = get.call_args.args[0]
            self.assertIn("image.pollinations.ai/prompt/", url)
            self.assertIn("a%20fox", url)

    def test_legacy_non_image_raises(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/html"}
        resp.text = "<html>paywall</html>"
        with patch.dict(os.environ, {}, clear=True), \
             patch("pollinations.requests.get", return_value=resp):
            with self.assertRaises(RuntimeError):
                pollinations.text_to_image("a fox")

    def test_key_path_uses_gen_endpoint(self):
        payload = base64.b64encode(b"key-bytes").decode()
        post_resp = MagicMock()
        post_resp.json.return_value = {"data": [{"b64_json": payload}]}
        with patch.dict(os.environ, {"POLLINATIONS_API_KEY": "test-key"}), \
             patch("pollinations.requests.post", return_value=post_resp) as post:
            out = pollinations.text_to_image("a fox")
            self.assertEqual(out, b"key-bytes")
            self.assertIn("gen.pollinations.ai", post.call_args.args[0])


class TestHordeClient(unittest.TestCase):
    def _webp_bytes(self):
        from PIL import Image
        img = Image.new("RGB", (64, 64), "red")
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        return buf.getvalue()

    def test_happy_path_converts_webp_to_jpeg(self):
        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"id": "job-1", "kudos": 6.0}
        status_resp = MagicMock()
        status_resp.json.return_value = {
            "done": True,
            "generations": [{"img": "https://x/img.webp", "state": "ok",
                             "censored": False, "model": "SDXL 1.0"}],
        }
        dl_resp = MagicMock()
        dl_resp.content = self._webp_bytes()
        with patch("aihorde.requests.post", return_value=post_resp), \
             patch("aihorde.requests.get",
                   side_effect=[status_resp, dl_resp]) as get:
            out = aihorde.text_to_image("a fox", poll_interval=0)
            # JPEG magic bytes
            self.assertTrue(out.startswith(b"\xff\xd8\xff"))
            # one status poll + one image download
            self.assertEqual(get.call_count, 2)

    def test_submit_path_is_generate_async(self):
        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"id": "job-1"}
        with patch("aihorde.requests.post", return_value=post_resp) as post, \
             patch("aihorde.requests.get",
                   side_effect=RuntimeError("stop after submit")):
            with self.assertRaises(RuntimeError):
                aihorde.text_to_image("a fox", timeout=0)
            url = post.call_args.args[0]
            self.assertTrue(url.endswith("/generate/async"), url)
            self.assertNotIn("text2img", url)

    def test_censored_generation_raises(self):
        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"id": "job-1"}
        status_resp = MagicMock()
        status_resp.json.return_value = {
            "done": True,
            "generations": [{"img": "https://x/img.webp", "state": "ok",
                             "censored": True}],
        }
        with patch("aihorde.requests.post", return_value=post_resp), \
             patch("aihorde.requests.get", return_value=status_resp):
            with self.assertRaises(RuntimeError):
                aihorde.text_to_image("a fox", poll_interval=0)

    def test_timeout_zero_raises_immediately(self):
        post_resp = MagicMock()
        post_resp.status_code = 202
        post_resp.json.return_value = {"id": "job-1"}
        with patch("aihorde.requests.post", return_value=post_resp), \
             patch("aihorde.requests.get") as get:
            with self.assertRaises(RuntimeError) as ctx:
                aihorde.text_to_image("a fox", timeout=0, poll_interval=0)
            self.assertIn("timed out", str(ctx.exception))
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
