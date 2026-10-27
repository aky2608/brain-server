# Shared constants imported by both Python code and referenced in JS.
# When changing EVENT_DEFAULT_MINUTES, also update the JS const in calendar.html.
EVENT_DEFAULT_MINUTES: int = 30

# 1min.ai model names for UNIFY_CHAT_WITH_AI.
# Claude models are unsupported on this account. Both verified working 2026-10-27.
ONEMIN_PRIMARY_MODEL: str = "gpt-4o-mini"
ONEMIN_FALLBACK_MODEL: str = "gpt-4.1-mini"
