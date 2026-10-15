"""Notifications sent when new updates are detected."""
import logging

import httpx

from .config import Notify

log = logging.getLogger("update-manager")


def _describe(item: dict) -> str:
    if item["kind"] == "docker":
        return f"{item['name']} ({item['source']}) - new image for {item['detail']}"
    parts = [f"{item['update_count']} package(s)"] if item["update_count"] else []
    parts += [f"{a['name']} {a['current']} -> {a['latest']}" for a in item.get("app_updates", []) if a["update"]]
    return f"{item['name']} ({item['source']}) - {', '.join(parts)}"


async def send_notifications(cfg: Notify, new_items: list[dict]) -> None:
    if not new_items:
        return
    lines = [_describe(i) for i in new_items]
    body = "\n".join(lines[:30]) + (f"\n... and {len(lines) - 30} more" if len(lines) > 30 else "")
    errors = await _send(cfg, f"{len(new_items)} new update(s) available", body, {"items": new_items})
    for err in errors:
        log.warning("Notification failed: %s", err)


async def send_test(cfg: Notify) -> list[str]:
    """Sends a test message to every configured channel; returns the errors."""
    if not (cfg.ntfy_url or cfg.discord_webhook or cfg.webhook_url):
        return ["no notification channel configured"]
    return await _send(cfg, "Update Manager test", "Notifications are working.", {"items": [], "test": True})


async def _send(cfg: Notify, title: str, body: str, payload: dict) -> list[str]:
    errors = []
    async with httpx.AsyncClient(timeout=15) as client:
        async def post(label: str, url: str, **kwargs):
            try:
                resp = await client.post(url, **kwargs)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:  # message would include the (secret) URL
                errors.append(f"{label}: HTTP {exc.response.status_code}")
            except Exception as exc:  # notifications must never break a check
                errors.append(f"{label}: {str(exc) or exc.__class__.__name__}")

        if cfg.ntfy_url:
            headers = {"Title": title, "Tags": "package"}
            if cfg.ntfy_token:
                headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
            await post("ntfy", cfg.ntfy_url, content=body.encode(), headers=headers)
        if cfg.discord_webhook:
            await post("Discord", cfg.discord_webhook, json={"content": f"**{title}**\n{body}"[:1990]})
        if cfg.webhook_url:
            await post("Webhook", cfg.webhook_url, json={"title": title, **payload})
    return errors
