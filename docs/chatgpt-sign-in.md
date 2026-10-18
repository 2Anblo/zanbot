# ChatGPT plan integration

## Architecture

The terminal and agent retain their existing interface. `Config.provider` dispatches requests in `llm.create_message`. The ChatGPT adapter converts text history to Responses input and normalizes completed output to the existing content/stop_reason shape. It forwards text deltas to `on_text`. A failed, incomplete, or interrupted response raises `APIError`, leaving history unchanged.

Authentication is independent of inference. The CLI opens the system browser for OpenAI's documented public-client dynamic registration flow. A loopback listener on `127.0.0.1` receives the callback. Each attempt uses fresh state, nonce, and S256 PKCE. The issued client ID is used for token exchange, and RS256 ID tokens are validated against OpenAI's JWKS, issuer, audience, expiry, subject, and nonce. Returning sign-in must match the selected account's client ID and subject. Plan inference requires the granted plan-use and resource scopes.

Credentials are isolated per issued client ID. A stable host ID is persisted before initial authorization. Fernet encrypts the credential file; the encryption key resides in the OS keyring. Atomic replacement and a cross-process lock protect updates and refresh-token rotation. Logout attempts revocation before clearing tokens while retaining registration metadata. Failed remote revocation is reported.

The provider sends full history to `https://api.openai.com/v1/responses` with OAuth Bearer authentication, `store=false`, and `stream=true`. No API-key billing fallback occurs. Models are selected explicitly from the account catalog rather than hard-coded. This remains a text-chat integration; no Codex runtime or agent framework is required.

## Verification

Offline tests cover encrypted storage, identity verification, scope requirements, refresh rotation, history conversion, streaming callbacks, incomplete responses, and preservation of failed-turn history. Existing Anthropic tests remain applicable. These tests do not establish live OAuth availability or plan entitlement. Verify manually with `--login`, `--models`, and a completed chat turn.

## Official references

- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts, renewal, and logout](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
