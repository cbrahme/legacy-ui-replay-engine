from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from src.models.artifact import ActionType


class GuardrailViolation(Exception):
    """Base exception for all security guardrail and policy violations."""
    pass


class DomainViolationError(GuardrailViolation):
    """Raised when an action attempts to navigate to a non-allowlisted domain."""
    def __init__(self, url: str, host: Optional[str], allowed_domains: List[str]):
        super().__init__(
            f"Navigation to host '{host}' is prohibited by domain allowlist. "
            f"Permitted hosts: {allowed_domains}. Target URL: {url}"
        )
        self.url = url
        self.host = host
        self.allowed_domains = allowed_domains


class ActionViolationError(GuardrailViolation):
    """Raised when an unapproved action type is dispatched."""
    def __init__(self, action: str, allowed_actions: Set[str]):
        super().__init__(
            f"Action '{action}' is prohibited by action allowlist. "
            f"Permitted actions: {sorted(list(allowed_actions))}"
        )
        self.action = action
        self.allowed_actions = allowed_actions


class IrreversiblePolicy(str, Enum):
    """Runtime enforcement policy for mutating or high-risk financial actions."""
    BLOCK_UNATTENDED = "BLOCK_UNATTENDED"
    ROUTE_TO_HUMAN = "ROUTE_TO_HUMAN"
    AUDIT_LOG = "AUDIT_LOG"


class GuardrailPolicy(BaseModel):
    """
    Security and execution safety policy governing browser automation.
    Enforces URL host allowlists, action whitelisting, and gating for
    irreversible financial operations.
    """
    model_config = ConfigDict(extra="forbid")

    allowed_domains: List[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost"],
        description="Permitted hostname origins for browser navigation"
    )
    allowed_schemes: List[str] = Field(
        default_factory=lambda: ["http", "https"],
        description="Permitted URL schemes"
    )
    allowed_actions: Set[ActionType] = Field(
        default_factory=lambda: {
            ActionType.NAVIGATE,
            ActionType.CLICK,
            ActionType.CLICK_COORDINATE,
            ActionType.FILL,
            ActionType.EXTRACT,
            ActionType.ASSERT,
            ActionType.WAIT,
        },
        description="Permitted action types executable by engine"
    )
    irreversible_policy: IrreversiblePolicy = Field(
        default=IrreversiblePolicy.BLOCK_UNATTENDED,
        description="Policy for handling is_irreversible steps"
    )

    def validate_url(self, url: str) -> None:
        """
        Validates that a URL conforms to allowed schemes and allowed domain origins.
        Raises DomainViolationError if disallowed. Relative paths (e.g. /members) are permitted.
        """
        if not url:
            return

        clean_url = url.strip()
        # Relative URLs are resolved against base_url and inherit base_url security
        if clean_url.startswith("/") or clean_url == "about:blank":
            return

        # Determine whether input has an explicit URI scheme or is a scheme-less target.
        # A colon indicates a port ONLY if followed by valid port digits (e.g. localhost:8000).
        # Non-numeric suffixes (e.g. localhost:payload, custom:data, javascript:alert(1)) are URI schemes.
        if "://" in clean_url:
            parsed = urlparse(clean_url)
        elif ":" in clean_url:
            _, rest = clean_url.split(":", 1)
            port_candidate = rest.split("/", 1)[0]
            if port_candidate.isdigit() and 1 <= int(port_candidate) <= 65535:
                parsed = urlparse(f"http://{clean_url}")
            else:
                parsed = urlparse(clean_url)
        else:
            parsed = urlparse(f"http://{clean_url}")

        # Validate scheme
        if parsed.scheme and parsed.scheme.lower() not in [s.lower() for s in self.allowed_schemes]:
            raise DomainViolationError(
                url=clean_url,
                host=parsed.scheme,
                allowed_domains=self.allowed_domains,
            )

        # Validate host origin
        host = parsed.hostname
        if host is None:
            # If no host could be parsed from absolute URL
            raise DomainViolationError(
                url=clean_url,
                host=None,
                allowed_domains=self.allowed_domains,
            )

        host_lower = host.lower().rstrip(".")
        is_allowed = False
        for allowed in self.allowed_domains:
            allowed_clean = allowed.lower().strip().rstrip(".")
            if not allowed_clean:
                continue

            # Universal wildcard
            if allowed_clean == "*":
                is_allowed = True
                break

            # Subdomain wildcard (*.example.com or .example.com)
            if allowed_clean.startswith("*.") or allowed_clean.startswith("."):
                base_domain = allowed_clean.lstrip("*.")
                # Match exact apex domain or strictly dot-prefixed subdomain
                if host_lower == base_domain or host_lower.endswith(f".{base_domain}"):
                    is_allowed = True
                    break
            else:
                # Exact host match (e.g. "localhost", "127.0.0.1", "app.example.com")
                if host_lower == allowed_clean:
                    is_allowed = True
                    break

        if not is_allowed:
            raise DomainViolationError(
                url=clean_url,
                host=host,
                allowed_domains=self.allowed_domains,
            )

    def validate_action(self, action: ActionType) -> None:
        """
        Validates that the given action type is permitted by policy.
        Raises ActionViolationError if disallowed.
        """
        if action not in self.allowed_actions:
            raise ActionViolationError(
                action=action.value if hasattr(action, "value") else str(action),
                allowed_actions={a.value for a in self.allowed_actions},
            )

    def evaluate_irreversible_step(
        self,
        is_irreversible: bool,
        allow_irreversible: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates whether an irreversible step can proceed.
        Returns:
            (can_proceed, reason_if_blocked)
        """
        if not is_irreversible:
            return True, None

        if allow_irreversible:
            return True, None

        if self.irreversible_policy == IrreversiblePolicy.BLOCK_UNATTENDED:
            return (
                False,
                "IRREVERSIBLE_ACTION_BLOCKED: Step is marked irreversible and unattended "
                "execution without explicit authorization (--allow-irreversible) is prohibited by policy."
            )

        if self.irreversible_policy == IrreversiblePolicy.ROUTE_TO_HUMAN:
            return (
                False,
                "IRREVERSIBLE_ACTION_REQUIRES_HUMAN: Step requires operator authorization."
            )

        return True, None
