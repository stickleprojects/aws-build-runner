"""GitHub App authentication with only the standard library: an RS256-signed JWT (RSA PKCS#1 v1.5,
SHA-256), then a short-lived installation token for one repository. Lambda's Python has no crypto
library, and bundling one means native wheels per architecture; signing is ~40 lines, so it's here.

Never logs the key, the JWT or tokens.
"""
import base64
import hashlib
import json
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
# DER prefix of the DigestInfo for SHA-256 (RFC 8017, section 9.2, note 1).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _der(data: bytes, i: int) -> tuple[int, int, int]:
    """(tag, start of content, end of content) of the DER element at data[i]."""
    tag, length = data[i], data[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[i:i + n], "big")
        i += n
    return tag, i, i + length


def _ints(data: bytes, start: int, end: int) -> list[int]:
    """The INTEGERs directly inside a SEQUENCE's content."""
    out, i = [], start
    while i < end:
        tag, s, e = _der(data, i)
        if tag == 0x02:
            out.append(int.from_bytes(data[s:e], "big"))
        i = e
    return out


def private_key(pem: str) -> tuple[int, int]:
    """(n, d) from a PKCS#1 ("RSA PRIVATE KEY", what GitHub issues) or PKCS#8 ("PRIVATE KEY") PEM."""
    lines = [l.strip() for l in pem.strip().splitlines()]
    body = base64.b64decode("".join(l for l in lines if l and not l.startswith("-----")))
    tag, s, e = _der(body, 0)
    if tag != 0x30:
        raise ValueError("not a DER SEQUENCE")
    if "BEGIN PRIVATE KEY" in pem:  # PKCS#8: version, algorithm, OCTET STRING holding the PKCS#1 key
        i = s
        for _ in range(2):
            i = _der(body, i)[2]
        tag, s, e = _der(body, i)
        if tag != 0x04:
            raise ValueError("PKCS#8 key without an OCTET STRING")
        body = body[s:e]
        tag, s, e = _der(body, 0)
    version, n, _e, d = _ints(body, s, e)[:4]
    if version != 0:
        raise ValueError("unsupported RSA key version")
    return n, d


def rs256(message: bytes, n: int, d: int) -> bytes:
    k = (n.bit_length() + 7) // 8
    t = SHA256_PREFIX + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, pem: str, now: float | None = None) -> str:
    now = int(time.time() if now is None else now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                                separators=(",", ":")).encode())
    n, d = private_key(pem)
    return f"{header}.{claims}.{_b64url(rs256(f'{header}.{claims}'.encode(), n, d))}"


def request(method: str, path: str, auth: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    req = urllib.request.Request(API + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": auth, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "build-runner"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def installation_token(app_id: str, pem: str, repo: str, permissions: dict) -> str:
    """A token (valid up to 1 hour) for this one repository, with only these permissions."""
    auth = f"Bearer {app_jwt(app_id, pem)}"
    status, inst = request("GET", f"/repos/{repo}/installation", auth)
    if status != 200:
        raise RuntimeError(f"App isn't installed on {repo} (HTTP {status})")
    status, tok = request("POST", f"/app/installations/{inst['id']}/access_tokens", auth,
                          {"repositories": [repo.split("/", 1)[1]], "permissions": permissions})
    if status != 201:
        raise RuntimeError(f"couldn't get an installation token (HTTP {status})")
    return tok["token"]


def revoke(token: str) -> None:
    request("DELETE", "/installation/token", f"token {token}")
