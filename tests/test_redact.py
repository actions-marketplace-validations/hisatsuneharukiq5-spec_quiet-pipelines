import json

from quiet_pipelines.redact import MASK, redact, secret_values_from_env

# Fake secrets assembled at runtime so the source file never contains a real-looking token.
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FINE = "github_pat_" + "11ABCDEFG0" + "x" * 30
ANT = "sk-ant-api03-" + "Zz9" * 12
OAI = "sk-proj-" + "Qw3" * 10
GKEY = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
YA29 = "ya29." + "a0AfH6SM" * 4
REFRESH = "1//0" + "gAbCdEfGhIjKlMnOpQrStUvWx"
GOCSPX = "GOCSPX-" + "AbCdEfGh1234567890"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
SLACK = "xoxb-" + "1234567890-abcdefghij"
HF = "hf_" + "AbCdEfGhIjKlMnOpQrStUv"
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36"


def _clean(text, env=None):
    out, counts = redact(text, env=env or {})
    return out, counts


def test_known_token_formats_are_masked():
    for secret in (GH, FINE, ANT, OAI, GKEY, YA29, REFRESH, GOCSPX, AWS, SLACK, HF, JWT):
        out, counts = _clean(f"value: {secret} end")
        assert secret not in out, secret[:6]
        assert MASK in out and counts


def test_url_credentials_and_headers():
    out, _ = _clean(f"remote https://{GH}@github.com/o/r.git and https://user:hunter2pass@host/x")
    assert GH not in out and "hunter2pass" not in out
    assert "https://[REDACTED]@github.com/o/r.git" in out
    out, _ = _clean("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\nx-api-key: supersecretvalue99")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out and "supersecretvalue99" not in out
    assert out.startswith("Authorization: Bearer [REDACTED]")


def test_assignments_and_json_fields():
    text = 'refresh_token="1abcDEF234ghiJKL" client_secret: s3cr3tValue password=hunter2x'
    out, _ = _clean(text)
    for s in ("1abcDEF234ghiJKL", "s3cr3tValue", "hunter2x"):
        assert s not in out


def test_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    out, counts = _clean(f"x\n{pem}\ny")
    assert "MIIEow" not in out and counts.get("private_key") == 1


def test_env_values_masked_even_without_known_shape():
    env = {"MY_SERVICE_TOKEN": "plainwordsecret", "DB_PASSWORD": "correct horse battery"}
    out, counts = redact("login with plainwordsecret / correct horse battery", env=env)
    assert "plainwordsecret" not in out and "correct horse battery" not in out
    assert counts["known_value"] == 2


def test_json_env_secret_fields_masked():
    tok = {"token": "short", "refresh_token": "rt-value-123456", "client_secret": "cs-value-abcdef", "token_uri": "https://oauth2.googleapis.com/token"}
    env = {"YT_TOKEN_JSON": json.dumps(tok)}
    out, _ = redact("got rt-value-123456 and cs-value-abcdef from https://oauth2.googleapis.com/token", env=env)
    assert "rt-value-123456" not in out and "cs-value-abcdef" not in out
    assert "https://oauth2.googleapis.com/token" in out  # URLs inside the JSON are not treated as secrets


def test_non_secret_env_names_are_not_used():
    env = {"PATH": "/usr/local/bin:/usr/bin", "GIT_AUTHOR_NAME": "Some Person", "PWD": "/home/runner/work"}
    assert secret_values_from_env(env) == []
    out, _ = redact("cd /home/runner/work by Some Person", env=env)
    assert out == "cd /home/runner/work by Some Person"


def test_benign_text_is_preserved():
    sha = "3f2a9c1e5b7d8f0a1c2e4b6d8f0a2c4e6b8d0f1a"   # lowercase hex git SHA
    text = f"commit {sha}\nmax_tokens=16000 input_tokens: 123456\nHTTP 403 quotaExceeded\nDuration: 57.3s"
    out, counts = _clean(text)
    assert out == text, counts


def test_high_entropy_blob_masked():
    blob = "Ab3" + "xYz9Qw" * 6
    out, counts = _clean(f"key {blob}")
    assert blob not in out and counts.get("high_entropy") == 1


def test_empty_input():
    assert redact("", env={}) == ("", {})
