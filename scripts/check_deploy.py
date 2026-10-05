"""Fail deployment preflight before any Cloudflare mutation. Never reads secrets."""

import argparse
from pathlib import Path
import sys
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]


def validate(config, *, require_closed=False):
    errors = []
    if config.get("name") != "veil-vault":
        errors.append("unexpected Worker name")
    if config.get("workers_dev") is not False or config.get("preview_urls") is not False:
        errors.append("workers.dev and preview URLs must be disabled")
    if config.get("routes") != [{"pattern": "api.veil-s.com", "custom_domain": True}]:
        errors.append("expected only the api.veil-s.com Custom Domain")
    bindings = config.get("d1_databases", [])
    if len(bindings) != 1 or bindings[0].get("binding") != "DB" or bindings[0].get("database_name") != "veil-vault":
        errors.append("expected a single veil-vault D1 binding named DB")
    else:
        raw = bindings[0].get("database_id", "")
        try:
            parsed = uuid.UUID(raw)
            if parsed.int == 0 or str(parsed) != raw:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            errors.append("replace the production D1 placeholder with the approved database ID")
    variables = config.get("vars", {})
    if variables.get("SERVICE_ENABLED") not in ("true", "false"):
        errors.append("SERVICE_ENABLED must be explicitly true or false")
    if require_closed and variables.get("SERVICE_ENABLED") != "false":
        errors.append("one-time closed deployment requires SERVICE_ENABLED=false")
    if "PUBLISH_TOKEN" in variables:
        errors.append("PUBLISH_TOKEN must be a Worker secret, never a config variable")
    if config.get("triggers", {}).get("crons") != ["0 * * * *"]:
        errors.append("expected hourly cleanup")
    if config.get("observability", {}).get("enabled") is not False:
        errors.append("review invocation-log privacy before enabling observability")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-closed", action="store_true")
    args = parser.parse_args()
    with (ROOT / "wrangler.production.toml").open("rb") as source:
        config = tomllib.load(source)
    errors = validate(config, require_closed=args.require_closed)
    if errors:
        for error in errors:
            print("Deployment blocked: " + error, file=sys.stderr)
        return 1
    print("Production config preflight passed; account, schema, secret, WAF and DNS still require verification")
    return 0


if __name__ == "__main__":
    sys.exit(main())
