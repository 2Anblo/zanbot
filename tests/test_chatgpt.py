from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import requests

from test_chat import wire_lines
from zanbot.agent import run_turn
from zanbot.chatgpt import read_response
from zanbot.chatgpt_auth import AuthError, CredentialStore, ISSUER, authorize, get_access_token, update_tokens, verify_identity
from zanbot.config import Config, load_config
from zanbot.llm import APIError


class ChatGPTTests(unittest.TestCase):
    def test_completed_with_empty_output_uses_finished_items(self):
        chunks = []
        item = {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}
        events = [
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"status": "completed", "output": []}},
        ]
        result = read_response(wire_lines(events), chunks.append)
        self.assertEqual(result["content"], [{"type": "text", "text": "Hello"}])
        self.assertEqual(chunks, ["Hello"])

    def test_finished_item_without_completed_is_not_success(self):
        events = [{"type": "response.output_item.done", "output_index": 0,
                   "item": {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}}]
        with self.assertRaises(ValueError):
            read_response(wire_lines(events))

    @patch("zanbot.chatgpt_auth.verify_identity", return_value={"sub": "user", "email": "test@example.com"})
    @patch("zanbot.chatgpt_auth.exchange")
    def test_browser_callback_validates_state_and_uses_issued_client(self, exchange, verify):
        exchange.return_value = {"access_token": "access", "id_token": "id", "expires_in": 3600,
                                 "scope": "resource.invoke chatgpt.tokens.use.direct"}
        workers = []
        statuses = []
        captured = {}

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            captured.update(query)

            def callback():
                redirect = query["redirect_uri"][0]
                for state in ("wrong-state", query["state"][0]):
                    with requests.get(redirect, params={"state": state, "code": "code", "client_id": "issued"}, timeout=5) as response:
                        statuses.append(response.status_code)

            worker = threading.Thread(target=callback)
            worker.start()
            workers.append(worker)
            return True

        with patch("zanbot.chatgpt_auth.webbrowser.open", side_effect=browser), patch("builtins.print"):
            account = authorize({}, "urn:uuid:test-host")
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual(statuses, [400, 200])
        self.assertEqual(captured["client_id"], ["dynamic_agent_client"])
        self.assertEqual(captured["agent_name_hint"], ["Zanbot"])
        self.assertEqual(captured["code_challenge_method"], ["S256"])
        self.assertEqual(exchange.call_args.args[0]["client_id"], "issued")
        self.assertEqual(exchange.call_args.args[0]["redirect_uri"], captured["redirect_uri"][0])
        self.assertEqual(account["client_id"], "issued")
        verify.assert_called_once_with("id", "issued", captured["nonce"][0])

    def test_chatgpt_does_not_require_anthropic_key(self):
        with patch.dict("os.environ", {}, clear=True):
            config = load_config("chatgpt", "available-model")
            self.assertEqual(config.api_key, "")
            self.assertEqual(config.provider, "chatgpt")
            with self.assertRaisesRegex(ValueError, "--models"):
                load_config("chatgpt")

    def test_success_streams_before_completion(self):
        chunks = []

        def events():
            yield from wire_lines([{"type": "response.output_text.delta", "delta": "Hello"}])
            self.assertEqual(chunks, ["Hello"])
            yield from wire_lines([{"type": "response.completed", "response": {
                "status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "Hello"}]}]}}])

        self.assertEqual(read_response(events(), chunks.append)["content"][0]["text"], "Hello")

    @patch("zanbot.chatgpt.get_access_token", return_value="test-oauth-token")
    @patch("zanbot.chatgpt.requests.post")
    def test_plan_request_and_failure_history(self, post, token):
        response = Mock(ok=True)
        response.iter_lines.return_value = wire_lines([
            {"type": "response.output_text.delta", "delta": "Partial"},
            {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}},
        ])
        context = post.return_value
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        history = [{"role": "user", "content": "Hi"},
                   {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}]
        original = list(history)
        with self.assertRaises(APIError):
            run_turn(history, "Continue", Config(api_key="", model="test", provider="chatgpt"))
        self.assertEqual(history, original)
        body = post.call_args.kwargs["json"]
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertNotIn("max_output_tokens", body)
        self.assertNotIn("previous_response_id", body)
        self.assertEqual(body["input"][1]["content"], "Hello")
        context.__exit__.assert_called_once()

    def test_interrupted_and_incomplete_are_errors(self):
        for events in ([], [{"type": "response.incomplete"}], [{"type": "error"}]):
            with self.subTest(events=events), self.assertRaises(ValueError):
                read_response(wire_lines(events))

    def test_scope_required_and_refresh_rotation(self):
        tokens = {"access_token": "new-access", "expires_in": 3600, "refresh_token": "new-refresh"}
        with self.assertRaises(AuthError):
            update_tokens({}, tokens)
        old = {"scopes": ["resource.invoke", "chatgpt.tokens.use.direct"], "refresh_token": "old"}
        updated = update_tokens(old, tokens)
        self.assertEqual(updated["refresh_token"], "new-refresh")
        self.assertEqual(old["refresh_token"], "old")

    def test_encrypted_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory, patch("zanbot.chatgpt_auth.HOME", Path(directory)), \
                patch("zanbot.chatgpt_auth.keyring.get_password", return_value=Fernet.generate_key().decode()):
            store = CredentialStore()
            data = store.read()
            data["secret"] = "never-store-as-plaintext"
            store.write(data)
            self.assertNotIn(b"never-store-as-plaintext", store.path.read_bytes())
            self.assertEqual(store.read(), data)

    @patch("zanbot.chatgpt_auth.exchange")
    @patch("zanbot.chatgpt_auth.CredentialStore")
    def test_refresh_uses_issued_client_and_saves_rotation(self, store_class, exchange):
        store = Mock()
        store.lock = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        store_class.return_value = store
        store.read.return_value = {"active": "issued", "accounts": {"issued": {
            "client_id": "issued", "access_token": "old", "refresh_token": "refresh",
            "expires_at": 0, "scopes": ["resource.invoke", "chatgpt.tokens.use.direct"]}}}
        exchange.return_value = {"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}
        self.assertEqual(get_access_token(), "new")
        self.assertEqual(exchange.call_args.args[0]["client_id"], "issued")
        self.assertEqual(store.write.call_args.args[0]["accounts"]["issued"]["refresh_token"], "rotated")

    def test_jwt_identity_validation(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {"iss": ISSUER, "aud": "issued", "sub": "user", "iat": time.time(),
                  "exp": time.time() + 60, "nonce": "expected"}
        client = Mock()
        client.get_signing_key_from_jwt.return_value.key = private.public_key()
        with patch("zanbot.chatgpt_auth.jwt.PyJWKClient", return_value=client):
            token = jwt.encode(claims, private, algorithm="RS256")
            self.assertEqual(verify_identity(token, "issued", "expected")["sub"], "user")
            for change in ({"iss": "wrong"}, {"aud": "wrong"}, {"exp": 1}, {"nonce": "wrong"}):
                bad = jwt.encode({**claims, **change}, private, algorithm="RS256")
                with self.subTest(change=change), self.assertRaises(AuthError):
                    verify_identity(bad, "issued", "expected")
            bad = jwt.encode(claims, "not-the-signing-key-0123456789012345", algorithm="HS256")
            with self.assertRaises(AuthError):
                verify_identity(bad, "issued", "expected")
