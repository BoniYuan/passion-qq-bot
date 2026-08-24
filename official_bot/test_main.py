import unittest
from botpy.message import GroupMessage

from official_bot.main import admin_help, claim_email, format_recent_errors, mentioned_member_openid, parse_balance_command


class ParseBalanceCommandTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_balance_command("/充值 A@EXAMPLE.COM 15", 1000), ("充值", "a@example.com", 15.0))

    def test_requires_amount(self):
        with self.assertRaisesRegex(ValueError, "用法"):
            parse_balance_command("/充值 a@example.com", 1000)

    def test_rejects_bad_email(self):
        with self.assertRaisesRegex(ValueError, "邮箱格式"):
            parse_balance_command("/充值 nope 15", 1000)

    def test_rejects_excessive_amount(self):
        with self.assertRaisesRegex(ValueError, "金额必须"):
            parse_balance_command("/退款 a@example.com 1001", 1000)

    def test_claim_requires_email_only(self):
        self.assertEqual(claim_email(" A@EXAMPLE.COM "), "a@example.com")
        self.assertIsNone(claim_email("/充值 a@example.com 1"))
        self.assertIsNone(claim_email("/报错 a@example.com"))

    def test_admin_help_matches_permissions(self):
        regular = admin_help(False)
        self.assertIn("领取 $15 测试额度", regular)
        self.assertNotIn("/充值", regular)
        super_help = admin_help(True)
        for command in ("/充值", "/退款", "/兑换码", "/报错", "/添加管理员", "/删除管理员", "/管理员列表"):
            self.assertIn(command, super_help)

    def test_formats_five_errors_without_sensitive_fields(self):
        items = [{"created_at": "2026-08-19T12:00:00", "model": "gpt-test", "error_owner": "platform", "severity": "P2", "status_code": 500, "message": "upstream failed", "client_ip": "127.0.0.1", "api_key": "secret"}] * 5
        result = format_recent_errors("a@example.com", items)
        self.assertEqual(result.count("upstream failed"), 5)
        self.assertEqual(result.count("分类：平台错误 | 状态码：500 | 级别：P2"), 5)
        self.assertNotIn("127.0.0.1", result)
        self.assertNotIn("secret", result)

    def test_uses_last_non_sender_mention_as_admin_target(self):
        class User:
            def __init__(self, member_openid):
                self.member_openid = member_openid

        class Message:
            author = User("sender")
            mentions = [User("bot"), User("target")]

        self.assertEqual(mentioned_member_openid(Message()), "target")

    def test_group_mention_accepts_id_field(self):
        self.assertEqual(GroupMessage._User({"id": "target"}).member_openid, "target")


if __name__ == "__main__":
    unittest.main()
