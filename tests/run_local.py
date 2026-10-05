"""Run local Wasm/D1 tests in fresh state, with only public dummy credentials.

Requires worker-build --release first and wrangler on PATH. Does not delete files,
reuse an existing database, access remote D1, or read user .dev.vars files.
"""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from local_api import TEST_PUBLISH_TOKEN

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (ROOT / "build/worker/shim.mjs").is_file():
        raise SystemExit("Run worker-build --release first")
    (ROOT / ".local").mkdir(exist_ok=True)
    state = Path(tempfile.mkdtemp(prefix="verify-ci-", dir=ROOT / ".local"))
    config = state / "wrangler.toml"
    config.write_text(
        'name = "veil-vault"\n'
        f'main = {json.dumps(str(ROOT / "build/worker/shim.mjs"))}\n'
        'compatibility_date = "2026-10-05"\n'
        'workers_dev = false\npreview_urls = false\nsend_metrics = false\n'
        '[[d1_databases]]\nbinding = "DB"\ndatabase_name = "veil-vault"\n'
        'database_id = "00000000-0000-0000-0000-000000000000"\n'
        '[triggers]\ncrons = ["0 * * * *"]\n'
        '[observability]\nenabled = false\n', encoding="utf-8",
    )
    env = os.environ.copy()
    # Local-only commands never need account credentials or inherited dev secrets.
    for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "PUBLISH_TOKEN"):
        env.pop(key, None)
    env["WRANGLER_SEND_METRICS"] = "false"
    env["CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV"] = "false"
    subprocess.run(
        ["wrangler", "d1", "execute", "veil-vault", "--config", str(config), "--local",
         "--persist-to", str(state), "--file", str(ROOT / "schema.sql")],
        cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL, timeout=60,
    )

    @contextmanager
    def server(enabled=None, token=None):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = ["wrangler", "dev", "--config", str(config), "--local", "--test-scheduled",
                   "--persist-to", str(state), "--ip", "127.0.0.1", "--port", str(port),
                   "--inspector-port", "0", "--log-level", "error"]
        if enabled is not None:
            command.extend(["--var", "SERVICE_ENABLED:" + enabled])
        if token is not None:
            command.extend(["--var", "PUBLISH_TOKEN:" + token])
        # Each server logs only synthetic fixtures; keep diagnostics if startup fails.
        with (state / ("worker-" + str(port) + ".log")).open("w") as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"Worker exited; see {state}")
                    try:
                        request(port, "GET", "/missing")
                        break
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.1)
                else:
                    raise RuntimeError(f"Worker startup timed out; see {state}")
                yield port
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    for enabled, token in ((None, None), ("false", TEST_PUBLISH_TOKEN), ("TRUE", TEST_PUBLISH_TOKEN),
                           ("true", None), ("true", "invalid-token")):
        with server(enabled, token) as port:
            status, _, body = request(port, "POST", "/api/secrets")
            assert status == 503 and json.loads(body) == {"error": "service_unavailable"}
            if enabled != "true":
                status, headers, body = request(port, "GET", "/api/secrets/aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa")
                assert status == 503 and headers.get("cache-control") == "no-store"
    print("PASS: disabled/missing/malformed configuration fails closed")
    # Stopping must not consume a previously created record. Receiver access is
    # independent of the publisher credential when the service itself is enabled.
    with server("true", TEST_PUBLISH_TOKEN) as port:
        status, _, body = request(port, "POST", "/api/secrets", b"pause-fixture",
                                 {"Content-Type": "text/plain", "Authorization": "Bearer " + TEST_PUBLISH_TOKEN})
        assert status == 201
        secret_id = json.loads(body)["id"]
    path = "/api/secrets/" + secret_id
    with server("false", TEST_PUBLISH_TOKEN) as port:
        assert request(port, "GET", path)[0] == 503
    with server("true", None) as port:
        status, _, body = request(port, "GET", path)
        assert status == 200 and body == b"pause-fixture"
        assert request(port, "GET", path)[0] == 404
    print("PASS: stopping preserves ciphertext; receivers need no publisher token")
    with server("true", TEST_PUBLISH_TOKEN) as port:
        subprocess.run(
            [sys.executable, str(ROOT / "tests/local_api.py"), "--persist-to", str(state),
             "--port", str(port)], cwd=ROOT, env=env, check=True, timeout=180,
        )
    print(f"Local test state retained at {state}; all Workers stopped")


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
