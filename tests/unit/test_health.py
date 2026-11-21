import unittest

from app.domain.health import health_payload, is_callback_platform


class HealthPayloadTest(unittest.TestCase):
    def test_reports_the_running_commit(self):
        # 部署工作流靠返回体里的 commit 判定新版本已上线（D3）；返回别的值会让部署被判失败。
        self.assertEqual(health_payload("a" * 40), {"status": "ok", "commit": "a" * 40})

    def test_missing_commit_is_reported_as_unknown_not_empty(self):
        # 空串会让“返回体含 SHA”的判定在 SHA 也为空时误通过。
        self.assertEqual(health_payload("")["commit"], "unknown")


class CallbackPlatformTest(unittest.TestCase):
    def test_accepts_the_two_v1_platforms(self):
        self.assertTrue(is_callback_platform("shopee"))
        self.assertTrue(is_callback_platform("tiktok-shop"))

    def test_rejects_anything_else_instead_of_guessing(self):
        # 填进平台后台的回调地址必须逐字对得上；近似的名字不能被当成某个平台接住。
        for bad in ("", "Shopee", "tiktok", "tiktok_shop", "lazada", " shopee"):
            with self.subTest(value=bad):
                self.assertFalse(is_callback_platform(bad))


if __name__ == "__main__":
    unittest.main()
