"""Local approval of pending actions: a web UI (`sqlguard serve`) and `sqlguard approvals ...`.

An agent that submits a write the policy marks "ask" leaves a pending action in the action
store. A person looks at the measured impact and the row diff, and approves or rejects it here.
Both front ends sit on `ReviewService`, which produces the proof `Guard.resolve` needs with
`HmacProofs`. The proof key lives in the memory of the process the person is running, never in
the store, so an agent that can only reach the store (or the MCP server) cannot make one.

This is a label for who decided, not multi-user authentication, and it cannot stop an agent
that runs as the same OS user with a shell: that needs the host's permission rules (see
`docs/limitations.md`).

The web parts (`app`, `security`) need the `ui` extra; `proof` and `service` do not.
"""

from sqlguard.approvals.proof import HmacProofs
from sqlguard.approvals.service import ReviewError, ReviewService

__all__ = ["HmacProofs", "ReviewError", "ReviewService"]
