"""What must not leave: credential shapes and personal data, on the way back.

The other direction of the same problem, and the harder one. An input arrives whole, so it
can be inspected before anything happens. A response arrives a token at a time, and the
decision about whether it may be delivered has to be made before it has all been seen.

**Shapes, not secrets.** Nothing here knows any real credential. Every rule matches a
*format* - `AKIA` and sixteen uppercase characters, `-----BEGIN PRIVATE KEY-----`, a
Luhn-valid card number - which is why this works against a model that has been told a
secret the gateway has never seen. It is also why the false positives are what they are: a
test fixture, a documentation example and a real key are the same string to a regex, and the
eval's benign set is where that cost gets counted rather than assumed.

**Two rules are cheap and worth more than the rest.** `tollgate_api_key` matches this
gateway's own tenant key format, and `google_api_key` matches its provider key's. Those are
the two credentials that are certainly credentials, and a response containing one means
something has gone wrong that no amount of prompt engineering should be able to cause.

**Validators, where a shape alone is not enough.** A sixteen-digit number is not a card
number; a Luhn-valid sixteen-digit number very likely is. An SSN-shaped string with an
impossible area number is a part number. Cheap arithmetic removes whole classes of false
positive that a longer regex cannot.

**No redaction.** A finding stops a response; it never edits one. Two reasons, and the
first is the project's central constraint: bodies stay byte-compatible with the provider, so
an application adopts this gateway by changing a base URL, and a gateway that rewrites
response text is no longer that. The second is that a redacted answer is one the tenant
cannot reproduce, cannot cache and cannot debug - it looks like the model behaving oddly
rather than like a policy decision, which is the worst way for a security control to appear.

**Incremental scanning keeps an overlap.** A credential split across two streamed events
appears in neither half. `Incremental` therefore re-scans the tail of what it has already
seen along with each new piece of text, so a match that straddles a boundary is found once
and reported once. The overlap is what the bound on containment is measured against; see
proxy/streaming.py for what the gateway does with the answer.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

# What a finding is about. Two values, because the two have different consequences: a leaked
# credential is an incident, and a leaked email address is a compliance question.
SECRET = "secret"
PII = "pii"


@dataclass(frozen=True)
class Rule:
    """One shape worth stopping a response over.

    `id` is what reaches the ledger, the metric and the eval table - never the text that
    matched it. A finding says "this response contained something shaped like an AWS access
    key", which is everything an operator needs and none of the customer's data.
    """

    id: str
    category: str
    pattern: re.Pattern[str]
    # Where a shape alone produces too many false positives to be useful. Given the whole
    # match, it decides whether this really is what it looks like.
    validate: Callable[[str], bool] | None = None


def digits(text: str) -> str:
    return "".join(character for character in text if character.isdigit())


def luhn(text: str) -> bool:
    """The check digit every payment card carries.

    This is what turns "sixteen digits" - an order number, a hash fragment, a serial - into
    "a card number". One pass of arithmetic removes almost every false positive a length
    rule produces, which is the difference between a rule worth having and a rule that gets
    switched off in its first week.
    """
    numbers = [int(character) for character in reversed(digits(text))]
    if not 13 <= len(numbers) <= 19:
        return False
    total = 0
    for index, value in enumerate(numbers):
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def plausible_ssn(text: str) -> bool:
    """Whether a `nnn-nn-nnnn` string is a number the SSA could have issued.

    Area 000, 666 and 900-999 are never issued, and neither group 00 nor serial 0000 exists.
    A part number or a version string shaped like an SSN usually fails one of those, and the
    alternative - flagging every nine digits with two dashes - is a rule nobody would keep.
    """
    area, group, serial = text.split("-")
    return (
        area not in {"000", "666"}
        and not area.startswith("9")
        and group != "00"
        and serial != "0000"
    )


def rule(
    id: str, category: str, pattern: str, validate: Callable[[str], bool] | None = None
) -> Rule:
    return Rule(id, category, re.compile(pattern), validate)


RULES: tuple[Rule, ...] = (
    # --- the two this gateway is certain about ------------------------------------------
    # A Tollgate tenant key. If one of these is in a response, a tenant has pasted its own
    # credential into a prompt and the model has repeated it back - which is a key that now
    # needs revoking, and the gateway is the only component positioned to notice.
    rule("tollgate_api_key", SECRET, r"\btg_[A-Za-z0-9_\-]{40,}\b"),
    # The provider key's own format. This one should be impossible: the key exists only in
    # the gateway's environment and is never in a prompt. A hit here is an incident.
    rule("google_api_key", SECRET, r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # --- other people's credentials ------------------------------------------------------
    rule("aws_access_key_id", SECRET, r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    # Forty base64 characters is far too common a shape to flag on its own - it is also a
    # hash, an id and half a sentence in base64 - so this one insists on the name beside it.
    rule(
        "aws_secret_access_key",
        SECRET,
        r"(?i)aws.{0,20}?secret.{0,20}?[\"'\s:=]+([A-Za-z0-9/+=]{40})\b",
    ),
    rule("github_token", SECRET, r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    rule("openai_api_key", SECRET, r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b"),
    rule("slack_token", SECRET, r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    rule("stripe_key", SECRET, r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    # The header, with something long enough after it to be a token rather than a word.
    rule("bearer_token", SECRET, r"(?i)authorization[\"'\s:=]+bearer\s+[A-Za-z0-9._\-]{20,}"),
    # A JWT's three dot-separated segments, the first of which always begins `eyJ` because
    # `{"` is what base64url encodes to.
    rule(
        "jwt",
        SECRET,
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
    ),
    # The header alone. Catching this is what makes the hold-back window in block mode worth
    # having: a private key is kilobytes long, but it announces itself in the first 40 bytes,
    # so the gateway can stop the stream before the key itself is relayed.
    rule(
        "private_key_block",
        SECRET,
        r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----",
    ),
    # --- personal data --------------------------------------------------------------------
    rule("email", PII, r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    # A separator is required. Without one this matches every ten-digit number there is -
    # timestamps, ids, quantities - and a rule that fires on a row count is not a PII rule.
    rule("phone", PII, r"(?:\+\d{1,2}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),
    rule("us_ssn", PII, r"\b\d{3}-\d{2}-\d{4}\b", plausible_ssn),
    # Thirteen to nineteen digits in card-like grouping, then the check digit decides.
    rule(
        "credit_card",
        PII,
        r"\b(?:\d[ \-]?){12,18}\d\b",
        luhn,
    ),
)

RULE_IDS = frozenset(item.id for item in RULES)
CATEGORIES = {item.id: item.category for item in RULES}

# How much of what has already been scanned is re-scanned with the next piece of text. Longer
# than the longest pattern can match, so a credential split across two streamed events is
# whole in one scan rather than in neither.
OVERLAP_CHARS = 256


def findings_in(text: str) -> list[str]:
    """Every rule that matched, by id, sorted. Ids only; see the module docstring."""
    found = []
    for item in RULES:
        for match in item.pattern.finditer(text):
            if item.validate is None or item.validate(match.group(0)):
                found.append(item.id)
                break
    return sorted(found)


class Incremental:
    """Scans a response as it arrives, keeping an overlap so nothing hides on a boundary.

    Stateful and single-use, one per response. `feed` returns the ids found for the first
    time in that call, so a caller can react to a new finding without re-reacting to the
    ones it has already handled - and `found` is the whole set at the end.
    """

    def __init__(self, overlap: int = OVERLAP_CHARS) -> None:
        self._overlap = overlap
        self._tail = ""
        self.found: set[str] = set()

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        # The tail of what came before, plus what just arrived. A match entirely inside the
        # tail was already found and is deduplicated by `found`, so re-scanning it costs a
        # little work and no correctness.
        scanned = self._tail + text
        self._tail = scanned[-self._overlap :]
        fresh = [item for item in findings_in(scanned) if item not in self.found]
        self.found.update(fresh)
        return sorted(fresh)

    @property
    def findings(self) -> list[str]:
        return sorted(self.found)
