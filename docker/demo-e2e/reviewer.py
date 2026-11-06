"""The reviewer of the end-to-end demo: a program that uses the real `sqlguard serve` web UI.

It signs in with the one-time link the server prints, reads the queue and an action's page
(the SQL, the policy reasons, the row diff the server recomputed from the snapshots), and
decides by submitting the same forms a browser would: the CSRF token and the proof are the ones
the page carries. Standard library only, so the demo needs nothing but Python on the host.

The parsing functions are pure and tested in tests/test_demo_e2e.py.
"""

from __future__ import annotations

import http.client
import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookies import SimpleCookie

VOID = frozenset({"meta", "link", "input", "br", "hr", "img"})
LINK = re.compile(
    r"http://(?P<host>[\w.\[\]:-]+?):(?P<port>\d+)(?P<base>/s/[\w-]+)/login\?token=(?P<token>[\w-]+)"
)


class Node:
    """A parsed element: tag, attributes, children and the text directly inside."""

    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None = None) -> None:
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node] = []
        self.content: list[str | Node] = []  # text and child elements, in document order

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def text(self) -> str:
        """All text under this element with whitespace collapsed."""
        return " ".join(
            " ".join(c if isinstance(c, str) else c.text() for c in self.content).split()
        )

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        found: list[Node] = []
        for child in self.children:
            if (tag is None or child.tag == tag) and (cls is None or cls in child.classes):
                found.append(child)
            found.extend(child.find_all(tag, cls))
        return found


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.current)
        self.current.children.append(node)
        self.current.content.append(node)
        if tag not in VOID:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        self.current.content.append(data)


def parse_html(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    return builder.root


def field_texts(cell: Node) -> list[str]:
    """`name=value` for each `<span class="field"><b>name</b> value</span>` in a diff cell."""
    out = []
    for span in cell.find_all("span", "field"):
        name = span.find_all("b")[0].text()
        out.append(f"{name}={span.text().removeprefix(name).strip()}")
    return out


@dataclass(frozen=True)
class SignIn:
    host: str
    port: int
    base: str  # the secret path prefix, /s/<random>
    token: str


def parse_signin_link(text: str) -> SignIn:
    """The sign-in link in the terminal output of `sqlguard serve`."""
    match = LINK.search(text)
    if match is None:
        raise ValueError("no sign-in link in the output of `sqlguard serve`")
    return SignIn(match["host"], int(match["port"]), match["base"], match["token"])


@dataclass(frozen=True)
class QueueRow:
    action_id: str  # the full 32-character id, from the link
    state: str


def parse_queue(html: str) -> list[QueueRow]:
    rows = []
    for tr in parse_html(html).find_all("tr"):
        link = next((a for a in tr.find_all("a") if "/a/" in (a.attrs.get("href") or "")), None)
        pill = next(iter(tr.find_all("span", "pill")), None)
        if link is not None and pill is not None:
            rows.append(QueueRow(link.attrs["href"].rsplit("/", 1)[-1], pill.text()))
    return rows


@dataclass(frozen=True)
class Form:
    action: str
    fields: dict[str, str]
    enabled: bool


@dataclass
class ActionPage:
    action_id: str
    state: str = ""
    table: str = ""
    requester: str = ""
    base_snapshot: str = ""
    head_snapshot: str = ""
    counts: str = ""
    rows: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    flashes: list[str] = field(default_factory=list)
    forms: dict[str, Form] = field(default_factory=dict)


def parse_action(html: str, action_id: str) -> ActionPage:
    """What the action page tells the reviewer, and the forms that decide it."""
    root = parse_html(html)
    page = ActionPage(action_id)
    h1 = root.find_all("h1")
    pills = h1[0].find_all("span", "pill") if h1 else []
    page.state = pills[0].text() if pills else ""
    facts = root.find_all("dl", "facts")
    if facts:
        terms = facts[0].find_all("dt")
        defs = facts[0].find_all("dd")
        for term, definition in zip(terms, defs, strict=False):
            name = term.text()
            if name == "Table":
                page.table = definition.text()
            elif name == "Requested by":
                page.requester = definition.text().split(" at ")[0]
            elif name == "Snapshots":
                codes = [c.text() for c in definition.find_all("code")]
                page.base_snapshot, page.head_snapshot = (codes + ["", ""])[:2]
    counts = root.find_all("p", "counts")
    page.counts = counts[0].text() if counts else ""
    for tr in root.find_all("tr"):
        change = next(
            (c.removeprefix("change-") for c in tr.classes if c.startswith("change-")), ""
        )
        if change:
            key, data = (td for td in tr.find_all("td")[1:3])
            cells = [data] if change != "modified" else [key, data]
            fields = [text for cell in cells for text in field_texts(cell)]
            page.rows.append(f"{change:<9} " + "  ".join(fields))
    for h2 in root.find_all("h2"):
        if h2.text() == "Why it needs a decision" and h2.parent is not None:
            siblings = h2.parent.children
            after = siblings[siblings.index(h2) + 1 :]
            for sibling in after:
                if sibling.tag == "ul":
                    page.reasons = [li.text() for li in sibling.find_all("li")]
                    break
                if sibling.tag == "h2":
                    break
    for section in root.find_all("section", "attention"):
        page.problems += [li.text() for li in section.find_all("li")]
    page.flashes = [p.text() for p in root.find_all("p", "flash")]
    for form in root.find_all("form"):
        action = form.attrs.get("action") or ""
        decision = action.rsplit("/", 1)[-1]
        if decision not in ("approve", "reject"):
            continue
        fields = {
            i.attrs["name"]: i.attrs.get("value") or ""
            for i in form.find_all("input")
            if i.attrs.get("name")
        }
        buttons = form.find_all("button")
        enabled = bool(buttons) and "disabled" not in buttons[0].attrs
        page.forms[decision] = Form(action, fields, enabled)
    return page


class UiError(RuntimeError):
    """The approval UI answered something the demo did not expect."""


class Reviewer:
    """A signed-in session on `sqlguard serve`, over plain HTTP on the published loopback port."""

    def __init__(self, signin: SignIn, host: str = "127.0.0.1") -> None:
        self.signin = signin
        self.host = host
        self.origin = f"http://{host}:{signin.port}"
        self.cookie_name = ""
        self.cookie_value = ""

    def _request(self, method: str, path: str, body: dict[str, str] | None = None):
        headers = {"Origin": self.origin}
        if self.cookie_name:
            headers["Cookie"] = f"{self.cookie_name}={self.cookie_value}"
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection(self.host, self.signin.port, timeout=30)
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, headers, response.read().decode("utf-8")
        finally:
            conn.close()

    def sign_in(self) -> None:
        """Open the one-time link and confirm, as a browser would; keeps the session cookie."""
        base, token = self.signin.base, self.signin.token
        status, _, _ = self._request("GET", f"{base}/login?token={token}")
        if status != 200:
            raise UiError(f"the sign-in page answered {status}")
        status, headers, _ = self._request("POST", f"{base}/login", {"token": token})
        cookie = SimpleCookie(headers.get("set-cookie", ""))
        if status != 303 or not cookie:
            raise UiError(f"sign-in was refused ({status}); the link works once")
        morsel = next(iter(cookie.values()))
        self.cookie_name, self.cookie_value = morsel.key, morsel.value

    @property
    def base(self) -> str:
        return self.signin.base

    def get(self, path: str) -> str:
        status, _, body = self._request("GET", f"{self.base}{path}")
        if status != 200:
            raise UiError(f"GET {path} answered {status}")
        return body

    def queue(self) -> list[QueueRow]:
        return parse_queue(self.get("/queue"))

    def page(self, action_id: str) -> ActionPage:
        return parse_action(self.get(f"/a/{action_id}"), action_id)

    def decide(self, page: ActionPage, decision: str, note: str) -> str:
        """Submit the page's own form (its CSRF token and proof) and return where it redirects."""
        form = page.forms.get(decision)
        if form is None or not form.enabled:
            raise UiError(f"the page offers no {decision} for action {page.action_id[:8]}")
        status, headers, _ = self._request("POST", form.action, {**form.fields, "note": note})
        if status != 303:
            raise UiError(f"{decision} answered {status}")
        return headers["location"]

    def flashes_at(self, location: str) -> list[str]:
        """The notice(s) the page at `location` shows right after a decision."""
        return parse_action(self.get(location[len(self.base) :]), "").flashes
