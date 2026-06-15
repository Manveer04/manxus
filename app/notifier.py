"""
ntfy.sh notification helper.
Sends push notifications for new orders.
"""
import os
import httpx

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else ""

PLATFORM_EMOJI = {
    "lazada": "🛒",
    "tiktok": "🎵",
    "shopee": "🍊",
}

def _short_name(name: str) -> str:
    """Shorten long product names if no group name was resolved."""
    if not name:
        return "Unknown Product"
    # If name is short enough already, use as-is
    if len(name) <= 40:
        return name
    # Split on common separators and take the first part
    for sep in ["|", "-", ","]:
        parts = name.split(sep)
        if len(parts) > 1:
            return parts[0].strip()
    return name[:40].strip()

async def notify_new_order(platform: str, order_id: str, buyer: str,
                           total: float, items: list,
                           action_url: str = "") -> bool:
    """
    items: list of {"name": str, "quantity": int}
    """
    emoji = PLATFORM_EMOJI.get(platform, "📦")

    if not NTFY_URL:
        print("[Notifier] NTFY_TOPIC not configured; skipping notification")
        return False

    # Group items by name and sum quantities
    grouped = {}
    for item in items:
        name = _short_name(item.get("name", "Unknown"))
        qty  = int(item.get("quantity", 1))
        grouped[name] = grouped.get(name, 0) + qty

    items_lines = "\n".join(f"  • {name} x{qty}" for name, qty in grouped.items())

    url_block = f"\n\nOpen Order: {action_url}" if action_url else ""

    message = (
        f"{emoji} {platform.title()} Order #{order_id}\n"
        f"Buyer: {buyer or 'Unknown'}\n"
        f"\n"
        f"{items_lines}\n"
        f"\n"
        f"Total: RM{total:.2f}"
        f"{url_block}"
    )

    headers = {
        "Title": f"New {platform.title()} Order!",
        "Priority": "high",
        "Tags": "shopping",
        "Content-Type": "text/plain; charset=utf-8",
    }
    if action_url:
        # ntfy clients open this URL directly when tapping the notification.
        headers["Click"] = action_url

    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                NTFY_URL,
                content=message.encode("utf-8"),
                headers=headers,
                timeout=10,
            )
        if r.status_code < 200 or r.status_code >= 300:
            print(f"[Notifier] ntfy HTTP {r.status_code} for order {order_id} on {platform}: {r.text[:200]}")
            return False
        print(f"[Notifier] Sent ntfy for order {order_id} on {platform}")
        return True
    except Exception as e:
        print(f"[Notifier] Failed to send ntfy: {e}")
        return False