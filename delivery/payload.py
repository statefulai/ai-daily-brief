"""Send-time email/group bodies. Never shrink a full edition to a summary card."""

from outputs import (
    build_email_send_parts,
    render_group_message,
    require_full_email_html,
)

__all__ = [
    "build_email_send_parts",
    "render_group_message",
    "require_full_email_html",
]
