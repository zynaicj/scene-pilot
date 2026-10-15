"""模型诊断安全性测试，不调用真实百炼服务。"""
import unittest
from scene_pilot.diagnostics import safe_error_details


class QuotaError(Exception):
    status_code = 403
    body = {"error": {"code": "AllocationQuota.FreeTierOnly", "message": "secret token"}}


class DiagnosticsTests(unittest.TestCase):
    def test_safe_provider_code(self):
        result = safe_error_details(QuotaError("private request"))
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["provider_code"], "AllocationQuota.FreeTierOnly")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("private", str(result))

    def test_redact_invalid_code(self):
        class UnsafeError(Exception):
            body = {"error": {"code": "api-key=private"}}
        self.assertEqual(safe_error_details(UnsafeError())["provider_code"], "unknown")


if __name__ == "__main__":
    unittest.main()
