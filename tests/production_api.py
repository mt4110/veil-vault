"""Run a bounded dummy-data smoke test against api.veil-s.com.

The publisher token is entered using a hidden terminal prompt, never an argument,
environment variable or file. This test does not change WAF or access D1 directly.
"""

import concurrent.futures
from datetime import datetime, timezone
import getpass
import http.client
import json
from pathlib import Path
import re
import ssl
import sys
import threading
import time
import uuid
import warnings


HOST = "api.veil-s.com"
PAYLOAD = b"veil-vault-production-test-dummy-ciphertext"


def request(method, path, body=None, token=None):
    # HTTPSConnection does not follow redirects or forward credentials elsewhere.
    connection = http.client.HTTPSConnection(HOST, timeout=15, context=ssl.create_default_context())
    headers = {"Content-Type": "text/plain; charset=utf-8"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()
    finally:
        connection.close()


def main():
    # Refuse getpass's echoed fallback when no interactive terminal is available.
    if not sys.stdin.isatty():
        print("ERROR: Run this script in an interactive terminal. Do not pipe a token.")
        return 1
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("PUBLISH_TOKEN (hidden input): ")
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        print("ERROR: Expected 64 lowercase hexadecimal characters. Value was not saved.")
        return 1

    report = {"host": HOST, "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}

    def expect(name, response, status, payload=None):
        actual, headers, body = response
        good = actual == status
        good = good and all(headers.get(k) == v for k, v in {
            "cache-control": "no-store", "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
        }.items())
        if payload is not None:
            good = good and body == payload
        report["checks"].append({"name": name, "status": actual, "passed": good})
        print(("PASS: " if good else "FAIL: ") + name + " (HTTP " + str(actual) + ")")
        if not good:
            raise ValueError("unexpected response")
        return body

    def create(name):
        body = expect(name, request("POST", "/api/secrets", PAYLOAD, token), 201)
        record = json.loads(body)
        parsed = uuid.UUID(record["id"])
        if parsed.version != 4 or record.get("expires_in_seconds") != 86400:
            raise ValueError("unexpected creation metadata")
        return "/api/secrets/" + str(parsed)

    try:
        # Separate batches to respect the existing 5 requests / 10 seconds WAF rule.
        time.sleep(12)
        expect("missing publisher token rejected", request("POST", "/api/secrets", PAYLOAD), 401)
        expect("invalid publisher token rejected", request("POST", "/api/secrets", PAYLOAD, "invalid"), 401)
        time.sleep(12)
        path = create("authenticated publish")
        expect("HEAD does not consume", request("HEAD", path), 405, b"")
        expect("first GET returns dummy ciphertext", request("GET", path), 200, PAYLOAD)
        expect("second GET returns 404", request("GET", path), 404)
        time.sleep(12)
        path = create("publish for concurrent consumption")
        time.sleep(12)
        barrier = threading.Barrier(4)

        def consume(_):
            barrier.wait(timeout=15)
            return request("GET", path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(consume, range(4)))
        statuses = [response[0] for response in responses]
        for index, response in enumerate(responses):
            status = response[0]
            expect("concurrent GET " + str(index + 1), response,
                   status if status in (200, 404) else 404, PAYLOAD if status == 200 else None)
        good = statuses.count(200) == 1 and statuses.count(404) == 3
        report["checks"].append({"name": "exactly one concurrent success", "passed": good})
        if not good:
            raise ValueError("concurrent result mismatch")
        report["passed"] = True
    except Exception:
        # No traceback, response body, bearer ID or token is written to output/report.
        report["passed"] = False
        print("FAIL: Test stopped. No consume operation is retried; WAF remains enabled.")
    finally:
        token = None
        root = Path(__file__).resolve().parents[1]
        directory = root / ".local"
        directory.mkdir(exist_ok=True)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        destination = directory / ("production-api-" + uuid.uuid4().hex + ".json")
        with destination.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
        print("Result: " + str(destination))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError, getpass.GetPassWarning):
        print("Cancelled. No token was saved.")
        sys.exit(1)
