"""Public-client OAuth for ChatGPT plan usage in open-source apps."""

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import webbrowser

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock
import jwt
import keyring
import requests

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
HOME = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "zanbot"


class AuthError(Exception):
    """A credential or authorization error safe to display."""


class CredentialStore:
    """Encrypt local records with a key held in the OS credential manager."""

    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = HOME / "chatgpt.enc"
        self.lock = FileLock(str(HOME / "chatgpt.lock"), timeout=10)

    def cipher(self):
        try:
            value = keyring.get_password("zanbot", "credential-encryption-key")
            if value is None:
                if self.path.exists():
                    raise AuthError("The credential encryption key is missing from the OS keyring.")
                value = Fernet.generate_key().decode("ascii")
                keyring.set_password("zanbot", "credential-encryption-key", value)
            return Fernet(value.encode("ascii"))
        except keyring.errors.KeyringError as exc:
            raise AuthError("A working OS credential manager is required for ChatGPT sign-in.") from exc

    def read(self):
        if not self.path.exists():
            return {"host_id": "urn:uuid:" + str(uuid.uuid4()), "accounts": {}, "active": None}
        try:
            return json.loads(self.cipher().decrypt(self.path.read_bytes()))
        except (InvalidToken, ValueError) as exc:
            raise AuthError("Stored credentials could not be read.") from exc

    def write(self, data):
        encrypted = self.cipher().encrypt(json.dumps(data).encode("utf-8"))
        temporary = self.path.with_suffix(".tmp")
        try:
            with open(temporary, "wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encrypted)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def exchange(form):
    try:
        with requests.post(TOKEN_URL, data={**form, "resource": RESOURCE}, timeout=(10, 30)) as response:
            if not response.ok:
                raise AuthError(f"Token exchange failed (HTTP {response.status_code}). Run --login again.")
            data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AuthError("Could not complete token exchange. Run --login again.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise AuthError("The token endpoint returned an invalid response.")
    if str(data.get("token_type", "")).lower() != "bearer":
        raise AuthError("The token endpoint did not return a Bearer token.")
    if not isinstance(data.get("expires_in"), (int, float)) or data["expires_in"] <= 0:
        raise AuthError("The token endpoint returned an invalid token lifetime.")
    return data


def verify_identity(token, client_id, nonce):
    try:
        keys = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", timeout=15)
        key = keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id,
                            issuer=ISSUER, leeway=5,
                            options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]})
        if not isinstance(claims["nonce"], str) or not secrets.compare_digest(claims["nonce"], nonce):
            raise AuthError("The sign-in nonce did not match.")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise AuthError("The ID token did not contain a valid account identity.")
        return claims
    except jwt.PyJWTError as exc:
        raise AuthError("The sign-in identity could not be verified.") from exc


def update_tokens(account, tokens):
    scopes = tokens.get("scope", " ".join(account.get("scopes", []))).split()
    if not {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(scopes):
        raise AuthError("ChatGPT plan access was not granted. Enable it during sign-in.")
    result = {**account, "access_token": tokens["access_token"],
              "expires_at": time.time() + tokens["expires_in"], "scopes": scopes}
    for name in ("refresh_token", "id_token"):
        if tokens.get(name):
            result[name] = tokens[name]
    return result


def authorize(account, host_id):
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log callback URLs or authorization codes.

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == "/auth/callback" and query.get("state") == [state]
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to Zanbot to check sign-in status." if valid else b"Invalid callback.")
            if valid:
                result.update(query)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        params = {"client_id": account.get("client_id", "dynamic_agent_client"),
                  "ext_agent_host_id": host_id, "response_type": "code", "redirect_uri": redirect,
                  "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
                  "code_challenge_method": "S256", "code_challenge": challenge}
        if account:
            if account.get("id_token"):
                params["id_token_hint"] = account["id_token"]
        else:
            params["agent_name_hint"] = "Zanbot"
        print("Continue with ChatGPT in your browser. Waiting up to five minutes...")
        if not webbrowser.open(ISSUER + "/api/accounts/authorize?" + urlencode(params)):
            raise AuthError("Could not open your browser. Configure a default browser and retry.")
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise AuthError("Sign-in timed out. Run --login to try again.")
    if "error" in result:
        raise AuthError("Sign-in was denied or could not be completed.")
    client_id = result.get("client_id", [account.get("client_id")])[0]
    if not client_id or client_id == "dynamic_agent_client" or len(result.get("code", [])) != 1:
        raise AuthError("Sign-in did not return a complete registration.")
    if account and client_id != account["client_id"]:
        raise AuthError("The callback client ID does not match the selected account.")
    tokens = exchange({"grant_type": "authorization_code", "client_id": client_id,
                       "code": result["code"][0], "code_verifier": verifier, "redirect_uri": redirect})
    if not isinstance(tokens.get("id_token"), str):
        raise AuthError("Sign-in did not return an ID token.")
    claims = verify_identity(tokens["id_token"], client_id, nonce)
    if account and claims["sub"] != account["subject"]:
        raise AuthError("The signed-in identity does not match the selected account.")
    return update_tokens({"client_id": client_id, "subject": claims["sub"],
                          "email": claims.get("email", "Unknown account")}, tokens)


def login(new=False):
    store = CredentialStore()
    # Serialize login and refresh across Zanbot processes, including token rotation.
    with store.lock:
        data = store.read()
        store.write(data)  # Persist the host ID before starting authorization.
        account = {} if new else data["accounts"].get(data["active"], {})
        account = authorize(account, data["host_id"])
        data["accounts"][account["client_id"]] = account
        data["active"] = account["client_id"]
        store.write(data)
    print(f"Signed in: {account['email']} ({account['client_id']})")


def get_access_token():
    store = CredentialStore()
    with store.lock:
        data = store.read()
        account = data["accounts"].get(data["active"], {})
        if not account.get("access_token"):
            raise AuthError("Not signed in. Run: uv run python main.py --login")
        if account["expires_at"] <= time.time() + 60:
            if not account.get("refresh_token"):
                raise AuthError("Your session expired. Run --login again.")
            tokens = exchange({"grant_type": "refresh_token", "client_id": account["client_id"],
                               "refresh_token": account["refresh_token"]})
            account = update_tokens(account, tokens)
            data["accounts"][data["active"]] = account
            store.write(data)
        return account["access_token"]


def accounts(select=None):
    store = CredentialStore()
    with store.lock:
        data = store.read()
        if select is not None:
            if select not in data["accounts"]:
                raise AuthError("Unknown account ID. Use --accounts to list saved accounts.")
            data["active"] = select
            store.write(data)
        for client_id, account in data["accounts"].items():
            marker = "*" if client_id == data["active"] else " "
            status = "signed in" if account.get("access_token") else "signed out"
            print(f"{marker} {client_id} | {account['email']} | {status}")
        if not data["accounts"]:
            print("No saved accounts. Run --login first.")


def logout():
    store = CredentialStore()
    with store.lock:
        data = store.read()
        account = data["accounts"].get(data["active"], {})
        confirmed = False
        if account.get("refresh_token"):
            try:
                with requests.get(ISSUER + "/.well-known/openid-configuration", timeout=15) as discovery:
                    discovery.raise_for_status()
                    endpoint = discovery.json()["revocation_endpoint"]
                if urlsplit(endpoint).scheme != "https" or urlsplit(endpoint).netloc != "auth.openai.com":
                    raise AuthError("Unexpected revocation endpoint.")
                for attempt in range(3):
                    try:
                        with requests.post(endpoint, data={"token": account["refresh_token"],
                                           "token_type_hint": "refresh_token", "client_id": account["client_id"]},
                                           timeout=15) as response:
                            confirmed = response.status_code == 200
                            if response.status_code < 500:
                                break
                    except requests.RequestException:
                        pass
                    time.sleep(2 ** attempt)
            except (requests.RequestException, ValueError, KeyError, AuthError):
                pass
        for name in ("access_token", "refresh_token", "id_token", "expires_at", "scopes"):
            account.pop(name, None)
        store.write(data)
    print("Signed out locally.")
    if not confirmed:
        print("Remote revocation was not confirmed. Disconnect Zanbot in ChatGPT Settings if needed.")
