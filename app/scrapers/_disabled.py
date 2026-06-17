from __future__ import annotations


class DisabledIntegrationError(RuntimeError):
    pass


def disabled_error(feature: str) -> DisabledIntegrationError:
    return DisabledIntegrationError(f"{feature} is disabled in this build")


async def disabled_async(feature: str, *args, **kwargs):
    del args, kwargs
    raise disabled_error(feature)


def disabled_sync(feature: str, *args, **kwargs):
    del args, kwargs
    raise disabled_error(feature)
