"""Per-supplier circuit breakers held in this process only.

A breaker never retries or waits. While open it fails fast with
``provider_circuit_open`` and an absolute ``retry_at``; after that time one
probe call is admitted. Business results (no tickets, bad arguments, unknown
station) neither open nor reset a breaker.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import httpx

from travel_tools.common import ToolFailure, utc_now

logger = logging.getLogger(__name__)

BROWSER_RUNTIME = "browser_runtime"
CIRCUIT_OPEN = "provider_circuit_open"

# Transient supplier faults: opened after consecutive occurrences.
FAILURE_CODES = {
    "provider_timeout",
    "upstream_timeout",
    "provider_network_error",
    "provider_unavailable",
    "provider_error",
    "provider_invalid_response",
    "upstream_invalid_response",
    "provider_catalog_unavailable",
    "provider_page_unavailable",
    "provider_browser_error",
}
RATE_CODES = {"provider_rate_limited"}
BLOCKED_CODES = {"provider_access_blocked"}
AUTH_CODES = {"provider_auth", "provider_authentication"}

Kind = Literal["failure", "rate", "blocked", "quota", "auth", "ignore"]


@dataclass(frozen=True)
class BreakerPolicy:
    enabled: bool = True
    failure_threshold: int = 3
    open_seconds: float = 60
    max_open_seconds: float = 600
    blocked_open_seconds: float = 600
    auth_open_seconds: float = 1800
    # A probe whose outcome never arrives must not keep the breaker shut forever.
    probe_lease_seconds: float = 120


class CircuitOpen(ToolFailure):
    def __init__(self, supplier: str, message: str, retry_at: datetime | None):
        super().__init__(CIRCUIT_OPEN, message, retryable=True)
        self.supplier = supplier
        self.retry_at = retry_at


def failure_kind(supplier: str, exc: BaseException) -> Kind:
    """Classify an exception seen at one supplier's I/O boundary."""
    if isinstance(exc, asyncio.CancelledError | CircuitOpen):
        return "ignore"
    scope = getattr(exc, "breaker_scope", None)
    if (scope is not None and scope != supplier) or (
        supplier == BROWSER_RUNTIME and scope != BROWSER_RUNTIME
    ):
        return "ignore"
    if isinstance(exc, ToolFailure):
        if exc.code in RATE_CODES:
            return "quota" if getattr(exc, "quota_exhausted", False) else "rate"
        if exc.code in BLOCKED_CODES:
            return "blocked"
        if exc.code in AUTH_CODES:
            return "auth"
        if exc.code in FAILURE_CODES:
            return "failure"
        if exc.code == "provider_http_error" and exc.retryable:
            return "failure"
        return "ignore"
    # Raw transport/parse errors that adapters translate outside the slot.
    if isinstance(exc, TimeoutError | httpx.TimeoutException | httpx.TransportError):
        return "failure"
    if isinstance(exc, ValueError | UnicodeError):
        return "failure"
    return "ignore"


_REASONS = {
    "failure": "连续超时或连接失败",
    "rate": "供应商限流",
    "quota": "供应商配额已用尽",
    "blocked": "供应商要求访问验证，已停止访问",
    "auth": "供应商拒绝凭据，请检查密钥或权限",
}


@dataclass
class _Breaker:
    state: Literal["closed", "open", "half_open"] = "closed"
    failures: int = 0
    cooldown: float = 0
    open_until: float = 0
    retry_at: datetime | None = None
    reason: str | None = None
    probe_started: float | None = None


class CircuitBreakers:
    def __init__(self, policy: BreakerPolicy | None = None, *, clock=time.monotonic, wall=utc_now):
        self.policy = policy or BreakerPolicy()
        self._clock = clock
        self._wall = wall
        self._breakers: dict[str, _Breaker] = {}

    def _get(self, supplier: str) -> _Breaker:
        return self._breakers.setdefault(supplier, _Breaker())

    def admit(self, supplier: str) -> bool:
        """Raise CircuitOpen or return True when this call is the half-open probe."""
        if not self.policy.enabled:
            return False
        breaker = self._breakers.get(supplier)
        if breaker is None or breaker.state == "closed":
            return False
        now = self._clock()
        if breaker.state == "open":
            if now < breaker.open_until:
                raise CircuitOpen(
                    supplier,
                    f"{supplier}来源暂时不可用（{breaker.reason}），已暂停访问；"
                    "可在retry_at之后再试，或使用其他来源。",
                    breaker.retry_at,
                )
            breaker.state = "half_open"
        if breaker.probe_started is not None and (
            now - breaker.probe_started < self.policy.probe_lease_seconds
        ):
            raise CircuitOpen(
                supplier,
                f"{supplier}来源正在试探恢复（{breaker.reason}），请稍后再试或使用其他来源。",
                None,
            )
        breaker.probe_started = now
        return True

    def release(self, supplier: str, probe: bool) -> None:
        """Outcome unknown (cancelled or business result): free the probe slot only."""
        if probe:
            breaker = self._get(supplier)
            breaker.probe_started = None

    def record_success(self, supplier: str, probe: bool) -> None:
        if not self.policy.enabled:
            return
        breaker = self._breakers.get(supplier)
        if breaker is None:
            return
        if breaker.state != "closed":
            logger.info("circuit closed supplier=%s", supplier)
        self._breakers[supplier] = _Breaker()

    def record_failure(self, supplier: str, exc: BaseException, probe: bool) -> None:
        if not self.policy.enabled:
            return
        kind = failure_kind(supplier, exc)
        if (
            supplier == BROWSER_RUNTIME
            and isinstance(exc, ToolFailure)
            and not isinstance(exc, CircuitOpen)
            and getattr(exc, "breaker_scope", None) != BROWSER_RUNTIME
        ):
            # The page itself failed, so the local browser did start and work.
            self.record_success(supplier, probe)
            return
        if kind == "ignore":
            self.release(supplier, probe)
            return
        breaker = self._get(supplier)
        breaker.probe_started = None
        p = self.policy
        if kind == "failure":
            breaker.failures += 1
            if breaker.state == "half_open":
                seconds = min(max(breaker.cooldown, p.open_seconds) * 2, p.max_open_seconds)
            elif breaker.state == "closed" and breaker.failures >= p.failure_threshold:
                seconds = p.open_seconds
            else:
                return
        else:
            seconds = {
                "rate": p.open_seconds,
                "quota": p.blocked_open_seconds,
                "blocked": p.blocked_open_seconds,
                "auth": p.auth_open_seconds,
            }[kind]
            hinted = getattr(exc, "retry_after_seconds", None)
            if kind == "rate" and isinstance(hinted, int | float) and hinted > 0:
                seconds = min(float(hinted), p.max_open_seconds)
            if breaker.state == "half_open":
                seconds = max(seconds, min(breaker.cooldown * 2, p.max_open_seconds))
        self._open(supplier, breaker, seconds, _REASONS[kind], getattr(exc, "code", kind))

    def _open(self, supplier, breaker, seconds, reason, code) -> None:
        breaker.state = "open"
        breaker.cooldown = seconds
        breaker.open_until = self._clock() + seconds
        breaker.retry_at = (self._wall() + timedelta(seconds=seconds)).replace(microsecond=0)
        breaker.reason = reason
        logger.warning(
            "circuit opened supplier=%s code=%s retry_at=%s",
            supplier,
            code,
            breaker.retry_at.isoformat(),
        )

    def blocked(self, supplier: str) -> dict | None:
        """Status for snapshots: None while calls would be admitted."""
        if not self.policy.enabled:
            return None
        breaker = self._breakers.get(supplier)
        if breaker is None or breaker.state != "open" or self._clock() >= breaker.open_until:
            return None
        return {"supplier": supplier, "reason": breaker.reason, "retry_at": breaker.retry_at}

    def state(self, supplier: str) -> str:
        breaker = self._breakers.get(supplier)
        return breaker.state if breaker else "closed"
