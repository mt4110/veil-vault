"""Exercise the actual Wasm Worker and a disposable local D1 database.

Start wrangler dev --local --test-scheduled with the same --persist-to path first.
Only dummy ciphertext is used. No remote D1 commands or database resets are run.
"""

import argparse
import concurrent.futures
import http.client as http_client
import json
from pathlib import Path
import subprocess
import threading
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--persist-to", required=True, type=Path)
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()
    state = args.persist_to.resolve()
    if not state.is_dir():
        parser.error("--persist-to must be an existing disposable local D1 directory")
    root = Path(__file__).resolve().parents[1]
    checks = 0

    def sql(query):
        result = subprocess.run(
            ["wrangler", "d1", "execute", "veil-vault", "--local", "--persist-to",
             str(state), "--command", query, "--json"],
            cwd=root, check=True, capture_output=True, text=True, timeout=30,
        )
        return json.loads(result.stdout)[0]["results"]

    def http(method, path, body=None, content_type="text/plain; charset=utf-8", chunked=False):
        connection = http_client.HTTPConnection("127.0.0.1", args.port, timeout=15)
        try:
            headers = {"Content-Type": content_type} if body is not None else {}
            if chunked:
                headers["Transfer-Encoding"] = "chunked"
                body = iter([body[:32768], body[32768:]])
            connection.request(method, path, body=body, headers=headers, encode_chunked=chunked)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def expect(response, status):
        nonlocal checks
        actual, headers, body = response
        assert actual == status, (actual, status, body)
        assert headers.get("cache-control") == "no-store", headers
        assert headers.get("x-content-type-options") == "nosniff", headers
        assert headers.get("referrer-policy") == "no-referrer", headers
        checks += 1
        return body

    def create(payload=b"dummy-ciphertext"):
        body = expect(http("POST", "/api/secrets", payload), 201)
        record = json.loads(body)
        assert uuid.UUID(record["id"]).version == 4
        assert record["expires_in_seconds"] == 86400
        return record["id"]

    # Verify the selected local DB is empty; never reuse a development/user DB.
    assert sql("SELECT count(*) AS n FROM secrets")[0]["n"] == 0, "Use a fresh test DB"

    payload = "dummy-日本語-ciphertext".encode()
    secret_id = create(payload)
    path = "/api/secrets/" + secret_id
    expect(http("HEAD", path), 405)
    expect(http("OPTIONS", path), 405)
    assert expect(http("GET", path), 200) == payload
    expect(http("GET", path), 404)
    assert sql(f"SELECT count(*) AS n FROM secrets WHERE id = '{secret_id}'")[0]["n"] == 0

    # Concurrent HTTP requests all execute the real DELETE RETURNING through D1.
    secret_id = create()
    path = "/api/secrets/" + secret_id
    barrier = threading.Barrier(24)

    def consume(_):
        barrier.wait(timeout=15)
        return http("GET", path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(consume, range(24)))
    statuses = [result[0] for result in results]
    assert statuses.count(200) == 1 and statuses.count(404) == 23, statuses
    for response in results:
        body = expect(response, response[0])
        if response[0] == 200:
            assert body == b"dummy-ciphertext"
    print("PASS: 24 concurrent GETs -> one 200 and 23 404s")

    # Exact 24-hour boundary must fail closed even before scheduled cleanup.
    expired_id = create()
    sql(f"UPDATE secrets SET created_at = unixepoch() - 86400 WHERE id = '{expired_id}'")
    expect(http("GET", "/api/secrets/" + expired_id), 404)
    assert sql(f"SELECT count(*) AS n FROM secrets WHERE id = '{expired_id}'")[0]["n"] == 0

    expired_id = create()
    active_id = create()
    sql(f"UPDATE secrets SET created_at = unixepoch() - 86401 WHERE id = '{expired_id}'")
    status, _, _ = http("GET", "/__scheduled?cron=0+*+*+*+*")
    assert status == 200, status
    rows = sql("SELECT id FROM secrets")
    ids = {row["id"] for row in rows}
    assert expired_id not in ids and active_id in ids, ids
    expect(http("GET", "/api/secrets/" + active_id), 200)
    print("PASS: expiry boundary and Cron cleanup preserve active records")

    expect(http("GET", "/api/secrets/not-a-uuid"), 400)
    expect(http("GET", "/api/secrets/" + str(uuid.uuid4())), 404)
    expect(http("GET", "/api/secrets/00000000-0000-0000-0000-000000000000"), 400)
    expect(http("GET", "/api/secrets"), 405)
    expect(http("GET", "/missing"), 404)
    expect(http("POST", "/api/secrets", b"{}", "application/json"), 415)
    expect(http("POST", "/api/secrets", b""), 400)
    expect(http("POST", "/api/secrets", b" \n\t"), 400)
    expect(http("POST", "/api/secrets", b"\xff"), 400)
    expect(http("POST", "/api/secrets", b"x" * 65537), 413)
    expect(http("POST", "/api/secrets", b"x" * 65537, chunked=True), 413)
    boundary_id = create(b"x" * 65536)
    assert expect(http("GET", "/api/secrets/" + boundary_id), 200) == b"x" * 65536
    print("PASS: input validation, 64 KiB boundary, and chunked size enforcement")

    # Fault injection is confined to this disposable local DB and dummy markers.
    failed_id = create()
    marker = "dummy-d1-failure-" + uuid.uuid4().hex
    sql(f"""CREATE TRIGGER reject_test_insert BEFORE INSERT ON secrets
        WHEN NEW.payload = '{marker}'
        BEGIN SELECT RAISE(ABORT, '{marker}'); END""")
    sql(f"""CREATE TRIGGER reject_test_consume BEFORE DELETE ON secrets
        WHEN OLD.id = '{failed_id}'
        BEGIN SELECT RAISE(ABORT, '{marker}'); END""")
    body = expect(http("POST", "/api/secrets", marker.encode()), 500)
    assert json.loads(body) == {"error": "internal_server_error"}
    body = expect(http("GET", "/api/secrets/" + failed_id), 500)
    assert json.loads(body) == {"error": "internal_server_error"}
    assert sql(f"SELECT count(*) AS n FROM secrets WHERE id = '{failed_id}'")[0]["n"] == 1
    print("PASS: D1 insert/delete failures return generic 500; failed delete preserves row")
    print(f"PASS: {checks} HTTP responses checked against local Wrangler/D1")


if __name__ == "__main__":
    main()
