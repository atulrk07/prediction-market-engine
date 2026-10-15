"""Best-effort Discord alerts evaluated after a worker snapshot commits."""

import json
import logging
import os
from urllib.request import Request, urlopen
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from models import alert_rules, markets, snapshots


DISCORD_TIMEOUT_SECONDS = 5


def send_discord_alert(rule, observation) -> bool:
    """Return true only after a successful response; never log the secret URL."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False
    content = (
        f"Price alert: {observation['title'][:500]} ({observation['ticker'][:200]})\n"
        f"YES ask: {observation['yes_ask']} | Threshold: {rule['threshold']}\n"
        f"Observed at: {observation['observed_at'].isoformat()}"
    )
    # urllib does not emit HTTPX's INFO logs, which include secret webhook URLs.
    # Preserve options such as thread_id, but always request confirmation.
    parts = urlsplit(webhook_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "wait"]
    query.append(("wait", "true"))
    confirmed_url = urlunsplit(parts._replace(query=urlencode(query)))
    request = Request(
        confirmed_url, method="POST",
        data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    # wait=true asks Discord to confirm the message was saved before responding.
    with urlopen(request, timeout=DISCORD_TIMEOUT_SECONDS) as response:
        delivered = 200 <= response.status < 300
        if not delivered:
            logging.error("Discord notification rejected rule=%s status=%s", rule["id"], response.status)
        return delivered


def process_snapshot_alerts(engine, snapshot_id: int) -> None:
    """Read committed values; each rule's state has its own transaction."""
    try:
        with engine.connect() as connection:
            observation = connection.execute(select(
                snapshots.c.market_id, snapshots.c.yes_ask, snapshots.c.observed_at,
                markets.c.title, markets.c.external_id.label("ticker"),
            ).join(markets, markets.c.id == snapshots.c.market_id).where(
                snapshots.c.id == snapshot_id,
            )).mappings().one()
            if observation["yes_ask"] is None:
                return
            rule_ids = connection.execute(select(alert_rules.c.id).where(
                alert_rules.c.market_id == observation["market_id"],
                alert_rules.c.enabled.is_(True),
            ).order_by(alert_rules.c.id)).scalars().all()
    except SQLAlchemyError:
        logging.error("Alert database read failed snapshot=%s", snapshot_id)
        return

    for rule_id in rule_ids:
        try:
            with engine.begin() as connection:
                # Recheck enabled and serialize with a concurrent disable request.
                rule = connection.execute(select(alert_rules).where(
                    alert_rules.c.id == rule_id, alert_rules.c.enabled.is_(True),
                ).with_for_update()).mappings().one_or_none()
                if rule is None:
                    continue
                if observation["yes_ask"] >= rule["threshold"]:
                    if rule["is_alerted"]:
                        connection.execute(alert_rules.update().where(alert_rules.c.id == rule_id).values(is_alerted=False))
                elif not rule["is_alerted"]:
                    try:
                        delivered = send_discord_alert(rule, observation)
                    except Exception:
                        # HTTP errors can include the webhook URL; never log them.
                        logging.error("Discord notification failed rule=%s", rule_id)
                        continue
                    if delivered:
                        connection.execute(alert_rules.update().where(alert_rules.c.id == rule_id).values(
                            is_alerted=True, last_alerted_at=func.now(),
                        ))
        except SQLAlchemyError:
            logging.error("Alert state database update failed rule=%s", rule_id)
