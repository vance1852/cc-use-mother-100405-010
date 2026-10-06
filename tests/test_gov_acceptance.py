import unittest

from science_strategy_foundation.gov_acceptance import run


class GovAcceptanceTest(unittest.TestCase):
    def test_offline_governance_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 第一次决议按提交时旧权重 3+2 定影
        self.assertEqual("approved", result["first_resolution"]["outcome"])
        self.assertEqual(5.0, result["first_resolution"]["yes_weight"])
        # 无资格成员被挡在表决之外
        self.assertTrue(result["ineligible_blocked"])
        # 许可账本后继事件链：授予→暂停→恢复→版本取代→新版本授予
        self.assertEqual(
            ["granted", "suspended", "resumed", "superseded", "granted"],
            result["grant_timeline"])
        # 下载回调去重，署名按贡献排序
        self.assertEqual(1, result["downloads"])
        self.assertTrue(result["callback_deduped"])
        self.assertEqual("cn", result["first_author"])
        # 退出成员的未履行承诺仍可被秘书处追溯
        self.assertEqual(["br"], result["unfulfilled_members"])


if __name__ == "__main__":
    unittest.main()
