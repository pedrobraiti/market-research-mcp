"""SEC EDGAR adapter — authoritative US filings and XBRL financials, from the source."""

from .edgar import USER_AGENT_ENV_VAR, SecEdgar, missing_user_agent_error

__all__ = ["USER_AGENT_ENV_VAR", "SecEdgar", "missing_user_agent_error"]
