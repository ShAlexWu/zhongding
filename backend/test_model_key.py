import hashlib
from unittest import TestCase, mock

from fastapi import HTTPException

import app
import config


class ModelKeyTest(TestCase):
    def test_update_requires_login_and_syncs_both_services(self) -> None:
        payload = app.ModelKeyRequest(api_key="example-api-key")
        original = config.DASHSCOPE_API_KEY
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        try:
            with mock.patch.object(app.auth, "auth_enabled", return_value=False):
                with self.assertRaises(HTTPException) as denied:
                    app.update_model_key(payload)
                self.assertEqual(denied.exception.status_code, 403)

            with (
                mock.patch.object(app.auth, "auth_enabled", return_value=True),
                mock.patch.object(config, "AUTH_PASSWORD", "test-internal-token"),
                mock.patch.object(app.urlrequest, "urlopen", return_value=response) as urlopen,
            ):
                result = app.update_model_key(payload)

            self.assertEqual(result, {"configured": True, "runtime_only": True})
            self.assertEqual(config.DASHSCOPE_API_KEY, "example-api-key")
            request = urlopen.call_args.args[0]
            self.assertEqual(
                request.get_header("X-internal-config-token"),
                hashlib.sha256(b"test-internal-token").hexdigest(),
            )
        finally:
            config.DASHSCOPE_API_KEY = original
