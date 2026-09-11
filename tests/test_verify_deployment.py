import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import publish_feed
import verify_deployment


class DeploymentVerifierTests(unittest.TestCase):
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def geturl(self):
            return verify_deployment.CANONICAL_FEED_URL

    class Opener:
        def __init__(self, payload):
            self.payload = payload
            self.request = None

        def open(self, request, timeout):
            self.request = request
            return DeploymentVerifierTests.Response(self.payload)

    def test_fetches_only_canonical_url_with_cache_revalidation(self):
        expected = (ROOT / "site/stable.json").read_bytes()
        opener = self.Opener(expected)
        actual = verify_deployment.fetch_once(
            opener, verify_deployment.CANONICAL_FEED_URL
        )
        self.assertEqual(actual, expected)
        headers = {name.lower(): value for name, value in opener.request.header_items()}
        self.assertEqual(headers["cache-control"], "no-cache, max-age=0")
        with self.assertRaisesRegex(publish_feed.PublishError, "canonical HTTPS"):
            verify_deployment.fetch_once(
                opener,
                verify_deployment.CANONICAL_FEED_URL + "?cache-bust=1",
            )

    def test_rejects_invalid_schema_and_oversized_response(self):
        invalid = json.dumps({"schemaVersion": 2}).encode()
        with self.assertRaises(publish_feed.PublishError):
            verify_deployment.validate_payload(invalid)
        oversized = b"x" * (publish_feed.MAX_FEED_BYTES + 1)
        with self.assertRaisesRegex(publish_feed.PublishError, "512 KiB"):
            verify_deployment.fetch_once(
                self.Opener(oversized), verify_deployment.CANONICAL_FEED_URL
            )

    def test_redirect_handler_refuses_redirects(self):
        self.assertIsNone(
            verify_deployment.NoRedirectHandler().redirect_request(
                None, None, 302, "Found", {}, "https://example.com/"
            )
        )


if __name__ == "__main__":
    unittest.main()
