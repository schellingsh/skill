#!/usr/bin/env python3
"""Directionally agent session client."""

import hashlib
import http.client
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone


VERSION = "0.2.10"
# sha256 of the SKILL.md that ships alongside this script. Regenerate with:
#   shasum -a 256 .agents/skills/directionally/SKILL.md
SKILL_SHA256 = "7004f9da2888162b0a3e14915103a04c0b53ebb1dde04e6ce1fba04bb239fb84"
DEFAULT_API_BASE = "https://api.directionally.ai"
DEFAULT_WEB_BASE = "https://directionally.ai"
DATA_DIR_ENV = "DIRECTIONALLY_DATA_DIR"
GLOBAL_DATA_DIR = os.path.join(os.path.expanduser("~"), ".directionally")
CREDENTIALS_PATH = os.path.join(GLOBAL_DATA_DIR, "credentials")
PENDING_LOGIN_PATH = os.path.join(GLOBAL_DATA_DIR, "pending_login")
AGENT_RUNTIME_PATH = os.path.join(GLOBAL_DATA_DIR, "agent")
SKILL_RELATIVE = os.path.join(".agents", "skills", "directionally", "SKILL.md")
SKILL_CLAUDE_RELATIVE = os.path.join(".claude", "skills", "directionally", "SKILL.md")
DEFAULT_SKILL_URL = (
    "https://raw.githubusercontent.com/directionallyai/skill/refs/heads/main"
    "/.agents/skills/directionally/SKILL.md"
)
DEFAULT_SCRIPT_URL = (
    "https://raw.githubusercontent.com/directionallyai/skill/refs/heads/main"
    "/.agents/skills/directionally/scripts/directionally.py"
)
# The published security declaration. Under sole-pin trust (DIR-285) the install
# command pins only this document's sha256; every artifact hash is then derived
# from the declaration itself rather than passed on the command line.
DEFAULT_DECL_URL = "https://directionally.ai/security-declaration.md"
CERTIFI_WHEEL_NAME = "certifi-2026.6.17-py3-none-any.whl"
CERTIFI_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/ef/2f/"
    "c5464532e965badff2f4c4c1a3a83f5697f0d7c407ed0cda44aaa99bb451/"
    "certifi-2026.6.17-py3-none-any.whl"
)
CERTIFI_WHEEL_SHA256 = "2227dcbaafe0d2f59279d1762ddddc37783ed4354594f194ffc31d20f41fc3db"
_TLS_CONTEXTS = {}

# Global skills dirs per agent type, matching the npx-skills registry.
# Each maps to ~/.{agent}/skills/ (or the agent's XDG equivalent).
def _global_skills_dir(agent_type, profile=None):
    home = os.path.expanduser("~")
    codex_home = os.environ.get("CODEX_HOME", "").strip() or os.path.join(home, ".codex")
    claude_home = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(home, ".claude")
    hermes_home = os.environ.get("HERMES_HOME", "").strip() or os.path.join(home, ".hermes")
    pi_home = os.environ.get("PI_HOME", "").strip() or os.path.join(home, ".pi")
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip() or os.path.join(home, ".config")
    # Hermes supports named profiles: the "default" profile's skills live directly
    # under <hermes_home>/skills, while every other profile is isolated under
    # <hermes_home>/profiles/<profile>/skills.
    if profile and profile != "default":
        hermes_skills = os.path.join(hermes_home, "profiles", profile, "skills")
    else:
        hermes_skills = os.path.join(hermes_home, "skills")
    mapping = {
        "claude-code":     os.path.join(claude_home, "skills"),
        "claude-desktop":  os.path.join(claude_home, "skills"),
        "codex":           os.path.join(codex_home, "skills"),
        "codex-desktop":   os.path.join(codex_home, "skills"),
        "cursor":          os.path.join(home, ".cursor", "skills"),
        "cursor-desktop":  os.path.join(home, ".cursor", "skills"),
        "hermes-agent":    hermes_skills,
        "opencode":        os.path.join(config_home, "opencode", "skills"),
        "pi":              os.path.join(pi_home, "agent", "skills"),
    }
    return mapping.get(agent_type)


def _skill_local_data_dir(agent_type, profile=None):
    if agent_type != "hermes-agent":
        return None
    hermes_skills = _global_skills_dir("hermes-agent", profile)
    return os.path.join(hermes_skills, "directionally", "data")


def _skill_local_runtime_path(agent_type, profile=None):
    if agent_type != "hermes-agent":
        return None
    hermes_skills = _global_skills_dir("hermes-agent", profile)
    return os.path.join(hermes_skills, "directionally", "scripts", "agent")


def verify_download(label, data, expected):
    """Refuse to install a tampered download.

    `data` is the raw bytes just pulled from the network; `expected` is the sha256
    we require it to match. Used at --setup time on the freshly downloaded SKILL.md
    (pinned by the hardcoded SKILL_SHA256) and directionally.py (pinned by the
    caller-supplied --script-hash, the trust anchor baked into the install command)."""
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        fail(f"integrity: {label} hash mismatch (expected {expected}, got {actual}). "
             "Refusing to install a tampered download.")


def parse_declaration_hashes(text):
    """Pull the pinned artifact sha256s out of the plain-English declaration.

    The declaration is prose, not a data format, so we look for a 64-hex token on
    the same line as each artifact name (the "What you are installing" section lists
    `install.py — sha256 <hex>` and `SKILL.md — sha256 <hex>`). The runtime is
    declared byte-identical to install.py, so it reuses that hash. update-install.sh
    asserts these stay in sync with the real artifacts, so the format can't drift
    silently."""
    hashes = {}
    for name in ("install.py", "SKILL.md"):
        for line in text.splitlines():
            if name in line:
                m = re.search(r"\b([0-9a-f]{64})\b", line)
                if m:
                    hashes[name] = m.group(1)
                    break
    return hashes


def get_api_base():
    return os.environ.get("DIRECTIONALLY_API_BASE", DEFAULT_API_BASE).rstrip("/")


def local_runtime_data_dir():
    path = globals().get("__file__")
    if not path:
        return None
    real_path = os.path.realpath(path)
    parent = os.path.dirname(real_path)
    if os.path.basename(real_path) == "agent" and os.path.basename(parent) == "data":
        return parent
    if os.path.basename(real_path) == "agent" and os.path.basename(parent) == "scripts":
        return os.path.join(os.path.dirname(parent), "data")
    return None


def data_dir():
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return os.path.realpath(os.path.expanduser(override))
    return local_runtime_data_dir() or GLOBAL_DATA_DIR


def agent_runtime_path():
    return os.path.join(data_dir(), "agent")


def _endpoint_hostname(api_base):
    parsed = urllib.parse.urlparse((api_base or DEFAULT_API_BASE).rstrip("/"))
    return parsed.hostname or "unknown"


def credential_path(api_base):
    if (api_base or DEFAULT_API_BASE).rstrip("/") == DEFAULT_API_BASE:
        return os.path.join(data_dir(), "credentials")
    return os.path.join(
        data_dir(),
        f"credentials-{_endpoint_hostname(api_base)}",
    )


def pending_login_path(api_base):
    if (api_base or DEFAULT_API_BASE).rstrip("/") == DEFAULT_API_BASE:
        return os.path.join(data_dir(), "pending_login")
    return os.path.join(
        data_dir(),
        f"pending_login-{_endpoint_hostname(api_base)}",
    )


def get_web_base(api_base=None):
    override = os.environ.get("DIRECTIONALLY_WEB_BASE", "").strip()
    if override:
        return override.rstrip("/")
    api_base = (api_base or get_api_base()).rstrip("/")
    parsed = urllib.parse.urlparse(api_base)
    if parsed.hostname == "api.dev.directionally.ai":
        return "https://dev.directionally.ai"
    return DEFAULT_WEB_BASE


def user_agent():
    return f"directionally/{VERSION}"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_ndjson(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def fail(message, code=1):
    sys.stderr.write(message + "\n")
    sys.exit(code)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _env_ca_file():
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(name, "").strip()
        if path:
            return path
    return None


def _has_default_ca_store():
    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.exists(paths.cafile):
        return True
    if paths.capath and os.path.isdir(paths.capath):
        try:
            if any(os.scandir(paths.capath)):
                return True
        except OSError:
            pass
    return False


def _certifi_cache_dir():
    return os.environ.get("DIRECTIONALLY_CERTIFI_CACHE", "").strip() or os.path.join(
        GLOBAL_DATA_DIR,
        "certifi"
    )


def write_ping():
    try:
        os.makedirs(data_dir(), exist_ok=True)
        with open(os.path.join(data_dir(), "ping"), "w") as _f:
            _f.write(str(random.randint(0, 2**31 - 1)))
    except OSError:
        # Some agents mount skill-local data read-only at runtime. Ping is only a
        # liveness breadcrumb, so read-only packaged data must not block use.
        pass


def _certifi_from_installed_package():
    try:
        import certifi  # type: ignore
    except Exception:
        return None
    try:
        path = certifi.where()
    except Exception:
        return None
    return path if path and os.path.exists(path) else None


def _download_pinned_certifi_wheel(cache):
    path = os.path.join(cache, CERTIFI_WHEEL_NAME)
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        if sha256_bytes(data) == CERTIFI_WHEEL_SHA256:
            return path
        try:
            os.remove(path)
        except OSError:
            pass

    if os.environ.get("DIRECTIONALLY_OFFLINE"):
        return None

    req = urllib.request.Request(CERTIFI_WHEEL_URL, headers={"User-Agent": user_agent()})
    # This artifact is pinned by sha256. Use an unverified TLS context only for
    # bootstrapping a CA bundle on Python installs that have no CA store at all.
    with urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context()) as resp:
        if resp.status != 200:
            raise RuntimeError(f"certifi download failed: HTTP {resp.status}")
        data = resp.read()
    got = sha256_bytes(data)
    if got != CERTIFI_WHEEL_SHA256:
        raise RuntimeError(
            f"certifi sha256 mismatch (expected {CERTIFI_WHEEL_SHA256}, got {got})"
        )
    os.makedirs(cache, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def _certifi_from_pinned_wheel():
    cache = _certifi_cache_dir()
    pem_path = os.path.join(cache, CERTIFI_WHEEL_NAME + ".cacert.pem")
    if os.path.exists(pem_path):
        return pem_path

    wheel_path = _download_pinned_certifi_wheel(cache)
    if not wheel_path:
        return None

    with zipfile.ZipFile(wheel_path) as zf:
        data = zf.read("certifi/cacert.pem")
    os.makedirs(cache, exist_ok=True)
    tmp = pem_path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, pem_path)
    return pem_path


def certifi_ca_file():
    cafile = _certifi_from_installed_package() or _certifi_from_pinned_wheel()
    if not cafile:
        raise RuntimeError(
            "No usable Python CA bundle found. Set SSL_CERT_FILE or install certifi."
        )
    return cafile


def tls_context(force_certifi=False):
    cafile = _env_ca_file()
    if force_certifi and not cafile:
        cafile = certifi_ca_file()
    elif not cafile and not _has_default_ca_store():
        cafile = certifi_ca_file()

    cache_key = cafile or "__default__"
    if cache_key not in _TLS_CONTEXTS:
        _TLS_CONTEXTS[cache_key] = ssl.create_default_context(cafile=cafile)
    return _TLS_CONTEXTS[cache_key]


def _is_cert_verification_error(exc):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    if reason is not None and _is_cert_verification_error(reason):
        return True
    for attr in ("__cause__", "__context__"):
        nested = getattr(exc, attr, None)
        if nested is not None and _is_cert_verification_error(nested):
            return True
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, BaseException) and _is_cert_verification_error(arg):
            return True
    return False


def _should_retry_with_certifi(exc):
    return not _env_ca_file() and _is_cert_verification_error(exc)


def connection_for(parsed, timeout, force_certifi=False):
    is_https = parsed.scheme == "https"
    host = parsed.hostname
    port = parsed.port or (443 if is_https else 80)
    if is_https:
        return http.client.HTTPSConnection(
            host, port, timeout=timeout, context=tls_context(force_certifi=force_certifi)
        )
    return http.client.HTTPConnection(host, port, timeout=timeout)


def request_with_tls_retry(parsed, timeout, method, path, body=None, headers=None):
    force_certifi = False
    while True:
        conn = connection_for(parsed, timeout=timeout, force_certifi=force_certifi)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            return conn, conn.getresponse()
        except Exception as e:
            conn.close()
            if parsed.scheme == "https" and not force_certifi and _should_retry_with_certifi(e):
                force_certifi = True
                continue
            raise


def urlopen_with_tls(req, timeout):
    url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
    if urllib.parse.urlparse(url).scheme != "https":
        return urllib.request.urlopen(req, timeout=timeout)

    try:
        return urllib.request.urlopen(req, timeout=timeout, context=tls_context())
    except Exception as e:
        if _should_retry_with_certifi(e):
            return urllib.request.urlopen(
                req, timeout=timeout, context=tls_context(force_certifi=True)
            )
        raise


def parse_args(args):
    flags = {"_": []}
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("--"):
            flags["_"].append(arg)
            i += 1
            continue
        if "=" in arg:
            key, val = arg[2:].split("=", 1)
            flags[key.replace("-", "_")] = val
        else:
            key = arg[2:].replace("-", "_")
            if not key:
                raise ValueError(f"Invalid flag: {arg}")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[key] = args[i + 1]
                i += 1
            else:
                flags[key] = True
        i += 1
    return flags


def number_flag(value, fallback, name):
    if value is None or value is True or value == "":
        return fallback
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative integer.")
    if n < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return n



def load_credential(api_base):
    path = credential_path(api_base)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("credential")
    except (OSError, json.JSONDecodeError):
        return None



def claude_settings_path():
    home = os.path.expanduser("~")
    claude_home = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(home, ".claude")
    return os.path.join(claude_home, "settings.json")


def add_runtime_permission_rule():
    """Grant a standing, prefix-scoped permission for the runtime path in the user's
    Claude Code settings — the visible, revocable replacement for the old frontmatter
    allowed-tools bypass.

    The rule is scoped to the stable runtime path (`Bash(<agent>:*)`) so a single grant
    covers --first / --session / upload without prompting per argument. Merges into any
    existing settings.json, never clobbering unrelated keys, and is idempotent. Returns
    (rule, path, action) where action is 'added', 'already-present', or 'skipped: <why>'."""
    agent_path = os.path.realpath(agent_runtime_path())
    rule = f"Bash({agent_path}:*)"
    path = claude_settings_path()

    settings = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read().strip()
            settings = json.loads(text) if text else {}
        except (OSError, json.JSONDecodeError) as exc:
            return rule, path, f"skipped: could not parse existing {path} ({exc}); leaving it untouched"
    if not isinstance(settings, dict):
        return rule, path, f"skipped: {path} is not a JSON object; leaving it untouched"

    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []

    if rule in allow:
        return rule, path, "already-present"

    allow.append(rule)
    permissions["allow"] = allow
    settings["permissions"] = permissions

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return rule, path, "added"


def save_credential(api_base, credential, username):
    path = credential_path(api_base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"credential": credential, "username": username}, f)
    os.chmod(path, 0o600)


def save_pending_login(api_base, token, url):
    path = pending_login_path(api_base)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"token": token, "url": url}, f)


def load_pending_login(api_base):
    path = pending_login_path(api_base)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def clear_pending_login(api_base):
    try:
        os.remove(pending_login_path(api_base))
    except OSError:
        pass


def try_redeem_pending_login(api_base):
    pending = load_pending_login(api_base)
    if not pending or not pending.get("token"):
        return
    parsed = urllib.parse.urlparse(api_base)
    try:
        poll_path = f"{parsed.path}/api/cli/login/poll?token={urllib.parse.quote(pending['token'], safe='')}"
        conn, resp = request_with_tls_retry(
            parsed, 10, "GET", poll_path, headers={"User-Agent": user_agent()}
        )
        if resp.status == 200:
            result = json.loads(resp.read())
            conn.close()
            status = result.get("status")
            if status == "granted":
                save_credential(api_base, result["credential"], result.get("username", ""))
                clear_pending_login(api_base)
            elif status == "expired":
                clear_pending_login(api_base)
        else:
            conn.close()
    except Exception:
        pass


def auth_headers(api_base):
    cred = load_credential(api_base)
    if cred:
        return {"Authorization": f"Bearer {cred}"}
    return {}


def _emit_login_needed(api_base):
    """Print a login URL and exit with the auth-failure sentinel that SKILL.md watches for."""
    # Reuse a previously saved pending token if we have one.
    pending = load_pending_login(api_base)
    login_url = pending.get("url") if pending else None

    if not login_url:
        parsed = urllib.parse.urlparse(api_base)
        try:
            conn, resp = request_with_tls_retry(
                parsed,
                15,
                "POST",
                f"{parsed.path}/api/cli/login/start",
                headers={"User-Agent": user_agent(), "Content-Length": "0"},
            )
            if resp.status == 200:
                data = json.loads(resp.read())
                login_url = data.get("url")
                if login_url:
                    save_pending_login(api_base, data.get("token", ""), login_url)
            conn.close()
        except Exception:
            pass

    if login_url:
        sys.stderr.write(
            f"Need to log in to Directionally. Open this URL in your browser:\n\n  {login_url}\n\n"
            "Then re-run your command.\n"
        )
    else:
        sys.stderr.write(
            "Need to log in to Directionally. Run: ~/.directionally/agent --login\n"
        )
    sys.exit(1)


def cmd_login(api_base):
    parsed = urllib.parse.urlparse(api_base)

    conn, resp = request_with_tls_retry(
        parsed,
        30,
        "POST",
        f"{parsed.path}/api/cli/login/start",
        headers={"User-Agent": user_agent(), "Content-Length": "0"},
    )
    if resp.status != 200:
        raise RuntimeError(f"login start failed: HTTP {resp.status}")
    data = json.loads(resp.read())
    conn.close()

    cli_token = data["token"]
    login_url = data["url"]
    expires_in = data.get("expires_in_seconds", 900)

    sys.stdout.write(f"\nOpen this URL to log in with GitHub:\n\n  {login_url}\n\nWaiting for login")
    sys.stdout.flush()

    deadline = time.time() + expires_in
    poll_interval = 3
    while time.time() < deadline:
        time.sleep(poll_interval)
        sys.stdout.write(".")
        sys.stdout.flush()
        poll_path = f"{parsed.path}/api/cli/login/poll?token={urllib.parse.quote(cli_token, safe='')}"
        conn, poll_resp = request_with_tls_retry(
            parsed, 15, "GET", poll_path, headers={"User-Agent": user_agent()}
        )
        if poll_resp.status == 200:
            result = json.loads(poll_resp.read())
            conn.close()
            status = result.get("status")
            if status == "granted":
                sys.stdout.write("\n")
                save_credential(api_base, result["credential"], result.get("username", ""))
                sys.stdout.write(f"Logged in as {result.get('username', 'unknown')}.\n")
                sys.stdout.write(f"Credential saved to {credential_path(api_base)}\n")
                return
            if status == "expired":
                raise RuntimeError("Login expired. Run --login again.")
        else:
            conn.close()

    raise RuntimeError("Login timed out. Run --login again.")



def write_if_changed(file_path, content):
    existed = os.path.exists(file_path)
    if existed:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                if f.read() == content:
                    return {"path": file_path, "action": "unchanged"}
        except OSError:
            pass
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": file_path, "action": "updated" if existed else "created"}


def render_skill_template(skill_body, runtime_ref=None):
    """Render local install values into the verified SKILL.md template."""
    agent_ref = runtime_ref or os.path.realpath(agent_runtime_path())
    rendered = skill_body.replace("~/.directionally/agent", agent_ref)
    if agent_ref == "scripts/agent" and "## Available scripts" not in rendered:
        rendered = rendered.replace(
            "## Runtime\n",
            "## Available scripts\n\n"
            "- **`scripts/agent`** — Directionally runtime for session start, polling, login, and upload.\n\n"
            "## Runtime\n",
            1,
        )
    return rendered


def download_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    with urlopen_with_tls(req, timeout=15) as resp:
        if resp.status != 200:
            raise ValueError(f"Could not download {url}: HTTP {resp.status}")
        return resp.read()


def download_skill(url):
    return download_url(url).decode("utf-8")


def exchange_install_token(api_base, token):
    """Exchange a one-time install token (embedded in the install command) for a
    long-lived CLI credential and save it. No separate --login step needed."""
    parsed = urllib.parse.urlparse(api_base)

    body = json.dumps({"token": token}).encode("utf-8")
    conn = None
    try:
        conn, resp = request_with_tls_retry(
            parsed,
            30,
            "POST",
            f"{parsed.path}/api/cli/install-token/exchange",
            body=body,
            headers={
                "User-Agent": user_agent(),
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        if resp.status != 200:
            err = resp.read(200).decode("utf-8", errors="replace")
            # Install tokens are single-use and short-lived (4h), so a 401 almost always
            # means the copied command sat too long or was already redeemed. Point the user
            # at the one place that mints a fresh one instead of leaving a bare HTTP error.
            if resp.status == 401:
                web_base = get_web_base(api_base)
                raise RuntimeError(
                    "Install token expired or already used. Visit "
                    f"{web_base} and click \"Install on your agent\" to get a new "
                    "installation command, then run that."
                )
            raise RuntimeError(
                f"Install token exchange failed: HTTP {resp.status}{': ' + err if err else ''}"
            )
        data = json.loads(resp.read())
    finally:
        if conn:
            conn.close()

    credential = data.get("credential")
    if not credential:
        raise RuntimeError("Install token exchange returned no credential.")
    username = data.get("username", "")
    save_credential(api_base, credential, username)
    return username


def fetch_active_pack_names(api_base):
    parsed = urllib.parse.urlparse(api_base)
    conn = None
    try:
        conn, resp = request_with_tls_retry(
            parsed,
            15,
            "GET",
            f"{parsed.path}/api/user/plan",
            headers={"User-Agent": user_agent(), **auth_headers(api_base)},
        )
        status = resp.status
        body = resp.read()
    finally:
        if conn:
            conn.close()

    if status < 200 or status >= 300:
        raise RuntimeError(f"plan lookup failed: HTTP {status}")

    data = json.loads(body)
    active = data.get("active_packs") or {}
    catalog = data.get("packs") or {}
    ids = []
    for group in ("standard", "premium", "private"):
        values = active.get(group) or []
        if isinstance(values, list):
            ids.extend(str(v) for v in values if v)

    seen = set()
    names = []
    for pack_id in ids:
        if pack_id in seen:
            continue
        seen.add(pack_id)
        pack = catalog.get(pack_id) or {}
        names.append(pack.get("display_name") or pack_id)
    return names


def print_post_setup_status(api_base):
    dashboard_url = f"{get_web_base(api_base)}/#/dashboard?section=packs"
    sys.stdout.write("\nDashboard: " + dashboard_url + "\n")
    try:
        names = fetch_active_pack_names(api_base)
    except Exception as exc:
        sys.stdout.write(
            "Active pack names: could not be loaded from your account yet "
            f"({exc}). Open the dashboard to confirm them.\n"
        )
        return

    if names:
        sys.stdout.write("Active pack names:\n")
        for name in names:
            sys.stdout.write(f"  - {name}\n")
    else:
        sys.stdout.write("Active pack names: none reported yet. Open the dashboard to choose packs.\n")


def find_session_trace():
    """Locate the current agent run's trace file from the host's session env var.

    Returns (trace_path_or_None, session_id_or_None, agent_type_or_None).
    Claude Code stores traces at ~/.claude/projects/<slug>/<CLAUDE_CODE_SESSION_ID>.jsonl;
    Codex stores them at ~/.codex/sessions/<Y>/<M>/<D>/rollout-*-<CODEX_THREAD_ID>.jsonl.
    """
    home = os.path.expanduser("~")
    claude_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    codex_tid = os.environ.get("CODEX_THREAD_ID", "").strip()

    if claude_sid:
        claude_home = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(home, ".claude")
        projects = os.path.join(claude_home, "projects")
        target = f"{claude_sid}.jsonl"
        for root, _dirs, files in os.walk(projects):
            if target in files:
                return os.path.join(root, target), claude_sid, "claude-code"
        return None, claude_sid, "claude-code"

    if codex_tid:
        codex_home = os.environ.get("CODEX_HOME", "").strip() or os.path.join(home, ".codex")
        sessions = os.path.join(codex_home, "sessions")
        for root, _dirs, files in os.walk(sessions):
            for name in files:
                if codex_tid in name and name.endswith(".jsonl"):
                    return os.path.join(root, name), codex_tid, "codex"
        return None, codex_tid, "codex"

    return None, None, None


def submit_trace(api_base, agent, trace_bytes):
    """POST a raw trace to the Directionally backend, authenticated with the CLI JWT.

    The backend stores it at v3/users/{email}/traces/{agent}/{sha256} and returns
    the storage key. `agent` is "claude" or "codex"."""
    parsed = urllib.parse.urlparse(api_base)

    path = f"{parsed.path}/submit_trace?agent={urllib.parse.quote(agent, safe='')}"
    conn = None
    try:
        conn, resp = request_with_tls_retry(
            parsed,
            120,
            "POST",
            path,
            body=trace_bytes,
            headers={
                "Content-Type": "application/x-ndjson",
                "User-Agent": user_agent(),
                "Content-Length": str(len(trace_bytes)),
                **auth_headers(api_base),
            },
        )
        status = resp.status
        body = resp.read()
    finally:
        if conn:
            conn.close()

    if status == 401:
        _emit_login_needed(api_base)
    if status < 200 or status >= 300:
        err = body.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"submit_trace failed: HTTP {status}{': ' + err if err else ''}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {}


def cmd_upload(api_base, flags):
    """Upload the current session's raw trace to the Directionally backend.

    Used when the user explicitly asks to share the session for review. Requires
    CLAUDE_CODE_SESSION_ID or CODEX_THREAD_ID in env to locate the trace."""
    trace_path, session_id, agent_type = find_session_trace()
    if not session_id:
        fail("upload: neither CLAUDE_CODE_SESSION_ID nor CODEX_THREAD_ID is set; "
             "cannot identify the session trace.")
    if not trace_path or not os.path.exists(trace_path):
        fail(f"upload: could not find a trace file for {agent_type} session {session_id}.")

    # Backend expects "claude" or "codex"; CLAUDE maps to claude-code internally.
    agent = "claude" if agent_type == "claude-code" else agent_type

    with open(trace_path, "rb") as f:
        trace_bytes = f.read()

    result = submit_trace(api_base, agent, trace_bytes)
    write_ndjson({
        "kind": "uploaded",
        "agent": agent,
        "session_id": session_id,
        "trace": trace_path,
        "hash": result.get("hash"),
        "key": result.get("key"),
        "received_at": now_iso(),
    })


def cmd_setup(flags):
    agent_type = flags.get("setup")
    if agent_type is True:
        agent_type = None
    api_base = get_api_base()

    # Optional --profile selects a named Hermes profile: "default" (or omitted)
    # installs into <hermes_home>/skills, anything else into that profile's dir.
    profile = flags.get("profile")
    if profile is True or profile == "":
        profile = None

    global_dir = _global_skills_dir(agent_type, profile) if agent_type else None
    if agent_type and not global_dir:
        raise ValueError(f"Unknown agent type: {agent_type!r}. Supported: claude-code, claude-desktop, codex, codex-desktop, cursor, cursor-desktop, hermes-agent, opencode, pi.")

    skill_dir = os.path.join(global_dir, "directionally") if global_dir else None
    skill_local_data = _skill_local_data_dir(agent_type, profile)
    if skill_local_data:
        os.environ[DATA_DIR_ENV] = skill_local_data
    runtime_path = _skill_local_runtime_path(agent_type, profile) or agent_runtime_path()
    runtime_ref = "scripts/agent" if skill_local_data else runtime_path

    # A one-time token embedded in the install command (DIR-83): exchange it for a
    # CLI credential before doing anything else, so identity is set up in one command.
    token = flags.get("token")
    if token and token is not True:
        username = exchange_install_token(api_base, token)
        sys.stdout.write(
            f"Authenticated as {username}.\n" if username else "Authenticated.\n"
        )

    # Expected sha256 of the agent runtime we are about to download, baked into
    # the install command by the trusted backend. Optional, but when present every
    # downloaded script is verified against it before being installed.
    script_hash = flags.get("script_hash")
    if script_hash is True or script_hash == "":
        script_hash = None

    # Sole-pin trust root (DIR-285): the install command pins only the declaration's
    # sha256. Re-fetch and re-verify it here (independently of the bootstrap that
    # exec'd us), then derive the artifact hashes from it. Declared hashes take
    # precedence over the command-line --script-hash and the hardcoded SKILL_SHA256.
    decl_hash = flags.get("decl_hash")
    if decl_hash is True or decl_hash == "":
        decl_hash = None
    decl_url = os.environ.get("DIRECTIONALLY_DECL_URL", DEFAULT_DECL_URL)
    declared = {}
    if decl_hash:
        decl_bytes = download_url(decl_url)
        verify_download("security-declaration.md", decl_bytes, decl_hash)
        declared = parse_declaration_hashes(decl_bytes.decode("utf-8"))

    skill_url = os.environ.get("DIRECTIONALLY_SKILL_URL", DEFAULT_SKILL_URL)
    skill_body = download_skill(skill_url)
    # The downloaded SKILL.md template must match the pinned hash — from the declaration
    # when sole-pinned, otherwise the hardcoded fallback baked into this script.
    # Install-specific values are rendered only after this verification step.
    verify_download("SKILL.md", skill_body.encode("utf-8"), declared.get("SKILL.md", SKILL_SHA256))
    rendered_skill_body = render_skill_template(skill_body, runtime_ref)

    script_url = os.environ.get("DIRECTIONALLY_SCRIPT_URL", DEFAULT_SCRIPT_URL)
    script_body = download_url(script_url)
    # The runtime is declared byte-identical to install.py, so its pin is the
    # install.py hash from the declaration; --script-hash still works standalone.
    effective_script_hash = script_hash or declared.get("install.py")
    if effective_script_hash:
        verify_download("directionally.py", script_body, effective_script_hash)

    files = []

    if global_dir:
        skill_dest = os.path.join(skill_dir, "SKILL.md")
        script_dest = runtime_path

        files.append({**write_if_changed(skill_dest, rendered_skill_body), "abs": skill_dest})
        files.append({**write_if_changed(script_dest, script_body.decode("utf-8")), "abs": script_dest})
        # Make the stable runtime directly executable so SKILL.md can invoke it
        # without exposing variable payloads in the command prefix.
        try:
            os.chmod(script_dest, 0o755)
        except OSError:
            pass

        lines = [f"Agent:  {agent_type}", f"Global: {global_dir}"]
        if skill_local_data:
            lines.append(f"Data:   {data_dir()}")
        lines.append("")
        for f in files:
            lines.append(f"  {f['action']:<9} {f['abs']}")
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        # No agent type: install into current project directory (legacy behaviour)
        cwd = flags.get("cwd")
        if not cwd or cwd is True:
            cwd = os.getcwd()
        target_root = os.path.realpath(cwd)

        files = [
            {**write_if_changed(os.path.join(target_root, SKILL_RELATIVE), rendered_skill_body), "rel": SKILL_RELATIVE},
            {**write_if_changed(os.path.join(target_root, SKILL_CLAUDE_RELATIVE), rendered_skill_body), "rel": SKILL_CLAUDE_RELATIVE},
            {**write_if_changed(runtime_path, script_body.decode("utf-8")), "rel": runtime_path},
        ]
        try:
            os.chmod(runtime_path, 0o755)
        except OSError:
            pass

        lines = [f"Root: {target_root}", ""]
        for f in files:
            lines.append(f"  {f['action']:<9} {f['rel']}")
        sys.stdout.write("\n".join(lines) + "\n")

    # Standing permission grant for the runtime path. Only Claude Code uses the
    # permissions.allow model in ~/.claude/settings.json; other agents fall back to
    # the per-session prefix request described in SKILL.md.
    if agent_type in (None, "claude-code", "claude-desktop"):
        rule, settings_path, action = add_runtime_permission_rule()
        if action == "added":
            sys.stdout.write(
                f'\nAdded "{rule}" to {settings_path} permissions.allow — this lets the '
                "Directionally runtime run without per-call prompts. Remove that line "
                "anytime to revoke.\n"
            )
        elif action == "already-present":
            sys.stdout.write(
                f'\nPermission rule "{rule}" already present in {settings_path} '
                "permissions.allow — left unchanged.\n"
            )
        else:
            sys.stdout.write(f"\nPermission rule not written: {action}\n")

    print_post_setup_status(api_base)


def open_first_session(api_base, initial_message):
    project_id = "v3/me"
    parsed = urllib.parse.urlparse(api_base)
    path = f"{parsed.path}/sessions/{urllib.parse.quote(project_id, safe='')}"

    body_parts = []
    if initial_message:
        body_parts.append(json.dumps(initial_message) + "\n")
    body = "".join(body_parts).encode("utf-8")

    conn, resp = request_with_tls_retry(
        parsed,
        120,
        "POST",
        path,
        body=body or None,
        headers={
            "Content-Type": "application/x-ndjson",
            "Accept": "application/x-ndjson",
            "User-Agent": user_agent(),
            "Content-Length": str(len(body)),
            **auth_headers(api_base),
        },
    )

    if resp.status == 401:
        resp.read()
        conn.close()
        _emit_login_needed(api_base)

    if resp.status < 200 or resp.status >= 300:
        err_body = resp.read(500).decode("utf-8", errors="replace")
        write_ndjson({
            "kind": "bridge_error",
            "error": f"HTTP {resp.status} {resp.reason}{': ' + err_body if err_body else ''}",
            "received_at": now_iso(),
        })
        sys.exit(1)

    sequence = 0

    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("kind") == "event_received" and isinstance(obj.get("sequence"), int):
            sequence = obj["sequence"]
            continue
        if obj.get("kind") == "session_started":
            write_ndjson({
                "kind": "bridge_started",
                "api_base": api_base,
                "session_id": obj["session_id"],
                "sequence": sequence,
                "received_at": now_iso(),
            })
            conn.close()
            return
        write_ndjson(obj)

    write_ndjson({
        "kind": "bridge_error",
        "error": "session stream ended before session_started",
        "received_at": now_iso(),
    })
    sys.exit(1)


def send_ops(session_id, api_base, ops):
    parsed = urllib.parse.urlparse(api_base)
    path = f"{parsed.path}/session/resume/{urllib.parse.quote(session_id, safe='')}"
    body = ("\n".join(json.dumps(op) for op in ops) + "\n").encode("utf-8")

    conn = None
    try:
        conn, resp = request_with_tls_retry(
            parsed,
            30,
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/x-ndjson",
                "User-Agent": user_agent(),
                "Content-Length": str(len(body)),
                **auth_headers(api_base),
            },
        )
        if resp.status < 200 or resp.status >= 300:
            err_body = resp.read(200).decode("utf-8", errors="replace")
            sys.stderr.write(f"directionally: ops not sent: HTTP {resp.status}{': ' + err_body if err_body else ''}\n")
        else:
            resp.readline()  # read first line then close; stream stays open server-side
    except Exception as e:
        sys.stderr.write(f"directionally: ops not sent: {e}\n")
    finally:
        if conn:
            conn.close()


def poll_session(api_base, flags):
    project_id = "v3/me"
    session_id = flags.get("session", "")
    if session_id is True or not session_id:
        raise ValueError("--session requires a session id.")
    session_id = str(session_id).strip()

    after = number_flag(flags.get("after"), 0, "--after")
    wait = number_flag(flags.get("wait"), 0, "--wait")
    limit = number_flag(flags.get("limit"), 100, "--limit")

    ops = []
    for arg in flags.get("_", []):
        try:
            ops.append(json.loads(arg))
        except json.JSONDecodeError:
            pass
    if ops:
        send_ops(session_id, api_base, ops)

    params = urllib.parse.urlencode({"after": after, "wait": wait, "limit": limit})
    url = (
        f"{api_base}/sessions/{urllib.parse.quote(project_id, safe='')}"
        f"/{urllib.parse.quote(session_id, safe='')}/events.ndjson?{params}"
    )
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/x-ndjson", "User-Agent": user_agent(), **auth_headers(api_base)},
    )
    try:
        with urlopen_with_tls(req, timeout=wait + 10) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}{chr(10) + err_body if err_body else ''}")

    if text:
        if not text.endswith("\n"):
            text += "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
    count = len([l for l in text.strip().splitlines() if l.strip()]) if text.strip() else 0
    write_ndjson({"kind": "polled", "count": count, "after": after, "received_at": now_iso()})


def usage(code=0):
    prog = "~/.directionally/agent"
    msg = "\n".join([
        "Usage:",
        f"  {prog} --login",
        f"  {prog} --setup <agent-type> [--token <tok>] [--decl-hash <sha256>] [--script-hash <sha256>] [--profile <name>]  # global install (claude-code, codex, cursor, hermes-agent, opencode, pi, ...)",
        f"  {prog} --setup [--cwd <path>] # project install (no agent type)",
        f"  {prog} upload  # gist the current session trace (needs CLAUDE_CODE_SESSION_ID/CODEX_THREAD_ID)",
        f"  {prog} --first --subsession-id <id> <text>",
        f"  {prog} --session <session_id> [--after <seq>] [--wait <secs>] [--limit <n>]",
        "",
        "  --setup verifies the downloaded SKILL.md and agent runtime before installing them.",
        "  With --decl-hash it re-fetches the security declaration, verifies it against that",
        "  sha256, and derives the SKILL.md and runtime hashes from it (sole-pin, DIR-285).",
        "  --script-hash still pins the runtime directly when used without a declaration.",
        "  --profile selects a named Hermes profile (hermes-agent only): 'default' (or omitted)",
        "  installs into <hermes_home>/skills, any other name into <hermes_home>/profiles/<name>/skills.",
        "",
        "Env:",
        f"  DIRECTIONALLY_API_BASE   Override API base URL (default: {DEFAULT_API_BASE})",
        "  DIRECTIONALLY_SKILL_URL  Override SKILL.md source URL used by --setup",
        f"  DIRECTIONALLY_DECL_URL   Override security-declaration.md URL (default: {DEFAULT_DECL_URL})",
    ])
    (sys.stdout if code == 0 else sys.stderr).write(msg + "\n")
    sys.exit(code)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        usage(0)

    try:
        flags = parse_args(args)
        if flags.get("setup") == "hermes-agent":
            profile = flags.get("profile")
            if profile is True or profile == "":
                profile = None
            skill_local_data = _skill_local_data_dir("hermes-agent", profile)
            if skill_local_data:
                os.environ[DATA_DIR_ENV] = skill_local_data

        write_ping()

        if flags.get("setup"):
            cmd_setup(flags)
            return

        api_base = get_api_base()
        try_redeem_pending_login(api_base)

        if flags.get("_") and flags["_"][0] == "upload":
            cmd_upload(api_base, flags)
            return

        if flags.get("login"):
            cmd_login(api_base)
            return

        if flags.get("first"):
            subsession_id = flags.get("subsession_id")
            if subsession_id is True:
                subsession_id = None
            text = " ".join(flags["_"]) if flags.get("_") else None
            initial_message = (
                {"op": "elaborating", "subsession_id": subsession_id, "text": text}
                if subsession_id and text
                else None
            )
            open_first_session(api_base, initial_message)
            return

        if flags.get("session"):
            poll_session(api_base, flags)
            return

        usage(1)

    except ValueError as e:
        fail(str(e))
    except RuntimeError as e:
        fail(str(e))
    except Exception as e:
        fail(str(e))


if __name__ == "__main__":
    main()
