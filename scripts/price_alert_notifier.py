#!/usr/bin/env python3
"""Standalone price-alert notifier for Jamaica Crop Intelligence.

Designed to run on a schedule (cron / GitHub Action) OUTSIDE the Streamlit app.
It builds the database, optionally auto-fetches official prices (works where the
network allows), computes the volatility watchlist + current price spikes, and
emits the result. If ``ALERT_WEBHOOK_URL`` is set it POSTs the JSON payload
there (e.g. a Slack/Teams/Make webhook) — otherwise it just prints JSON.

Honesty: it only sends what it computed. On illustrative seed data the payload
is labelled accordingly; ingest/fetch official prices for real signals.

Usage:
    python scripts/price_alert_notifier.py --threshold 15 --top 5 [--fetch]

Environment:
    ALERT_WEBHOOK_URL   optional; if set, payload is POSTed here as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jamaica_crop_intelligence.ingestion import official_fetch  # noqa: E402
from jamaica_crop_intelligence.services.alert_service import AlertService  # noqa: E402
from jamaica_crop_intelligence.services.import_service import ImportService  # noqa: E402
from jamaica_crop_intelligence.services.repository import Repository  # noqa: E402


def _maybe_fetch_prices(repo: Repository) -> list[str]:
    """Try to auto-fetch + commit any reachable official price sources."""
    notes: list[str] = []
    imp = ImportService(repo)
    for source in official_fetch.fetchable_sources():
        if source.get("kind") != "prices":
            continue
        out = official_fetch.fetch_and_preview(imp, source)
        if not out["ok"]:
            notes.append(f"fetch skipped ({source['name']}): {out['fetch'].error}")
            continue
        prev = out["preview"]
        res = imp.commit_preview(out["source_file_id"], "prices",
                                 prev["normalized"], prev["quality_issues"])
        notes.append(f"ingested {res['rows_committed']} rows from {source['name']}")
    return notes


def build_payload(threshold: float, top: int, do_fetch: bool) -> dict:
    repo = Repository.create(":memory:", seed=True)
    fetch_notes: list[str] = []
    if do_fetch:
        fetch_notes = _maybe_fetch_prices(repo)

    alerts = AlertService(repo)
    wl = alerts.watchlist(threshold_pct=threshold, n=top)
    any_official = repo.scalar(
        "SELECT COUNT(*) FROM price_records WHERE provenance='official_observed'") or 0
    return {
        "threshold_pct": threshold,
        "top_n": top,
        "data_basis": "official_observed" if any_official else "illustrative_seed",
        "fetch_notes": fetch_notes,
        "top_volatile": wl["top_volatile"].to_dict("records"),
        "spikes": wl["spikes"].to_dict("records"),
    }


def _post_webhook(url: str, payload: dict) -> int:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return getattr(resp, "status", 200)


def main() -> int:
    ap = argparse.ArgumentParser(description="Price-alert notifier")
    ap.add_argument("--threshold", type=float, default=15.0)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--fetch", action="store_true",
                    help="attempt to auto-fetch official prices first")
    args = ap.parse_args()

    payload = build_payload(args.threshold, args.top, args.fetch)
    text = json.dumps(payload, indent=2, default=str)

    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        try:
            status = _post_webhook(webhook, payload)
            print(f"posted to webhook (HTTP {status})", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"webhook post failed: {exc}", file=sys.stderr)
            print(text)
            return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
