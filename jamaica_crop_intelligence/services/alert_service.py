"""Price-spike and volatility alerting.

Detects period-on-period price spikes and ranks the most volatile crops, writes
alerts into the ``alerts`` table (deduped), and exposes them for the UI and for
an external notifier (e.g. a scheduled GitHub Action) to deliver.

Delivery note
-------------
This engine *detects and records* alerts. Actual push/email delivery requires a
channel configured outside the app (documented in the README). The app never
claims to have sent a notification it did not send.
"""

from __future__ import annotations

import pandas as pd

from .price_service import PriceService
from .repository import Repository


class AlertService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.prices = PriceService(repo)

    # ---------------------------------------------------------------- detect
    def price_spikes(self, threshold_pct: float = 15.0) -> pd.DataFrame:
        """Latest period-on-period % change per crop, flagged if |change| >= threshold."""
        rows = []
        crops = self.repo.query("SELECT id, canonical_name FROM crops")
        for c in crops:
            m = self.prices.change_metrics(c["id"])
            if "change_pct" not in m:
                continue
            change = m["change_pct"]
            if abs(change) >= threshold_pct:
                rows.append({
                    "crop": c["canonical_name"],
                    "latest_jmd_per_kg": m["latest_jmd_per_kg"],
                    "prev_jmd_per_kg": m.get("prev_jmd_per_kg"),
                    "change_pct": change,
                    "direction": "spike up" if change > 0 else "drop",
                    "period": m.get("latest_period"),
                })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.reindex(df["change_pct"].abs().sort_values(ascending=False).index)
            df = df.reset_index(drop=True)
        return df

    def top_volatile(self, n: int = 5) -> pd.DataFrame:
        """Top-N most price-volatile crops (coefficient of variation)."""
        return self.prices.most_volatile(top=n)

    # ----------------------------------------------------------------- write
    def generate_alerts(self, threshold_pct: float = 15.0) -> int:
        """Persist spike alerts into the alerts table (deduped by title). Returns count."""
        spikes = self.price_spikes(threshold_pct)
        created = 0
        for _, r in spikes.iterrows():
            title = f"Price {r['direction']}: {r['crop']} {r['change_pct']:+.0f}%"
            exists = self.repo.query_one(
                "SELECT 1 FROM alerts WHERE title=?", (title,))
            if exists:
                continue
            level = "warning" if abs(r["change_pct"]) >= max(threshold_pct, 25) else "watch"
            cid = self.repo.crop_id_by_name(r["crop"])
            self.repo.insert("alerts", {
                "level": level, "title": title,
                "body": (f"{r['crop']} moved from {r.get('prev_jmd_per_kg')} to "
                         f"{r['latest_jmd_per_kg']} JMD/kg ({r['change_pct']:+.1f}%) "
                         f"in {r.get('period')}. Modelled from ingested/seed prices."),
                "crop_id": cid,
            })
            created += 1
        return created

    def recent_alerts(self, limit: int = 50) -> pd.DataFrame:
        return self.repo.df(
            "SELECT level, title, body, created_at FROM alerts "
            "ORDER BY created_at DESC LIMIT ?", (limit,))

    def watchlist(self, threshold_pct: float = 15.0, n: int = 5) -> dict:
        """Bundle for a notifier: top volatile + current spikes."""
        return {
            "top_volatile": self.top_volatile(n),
            "spikes": self.price_spikes(threshold_pct),
        }
