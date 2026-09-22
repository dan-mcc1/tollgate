"""The regex baseline: the thing the classifier has to beat.

It is here to be beaten, and it stays here afterwards. A detector quoted without a
baseline beside it is an unfalsifiable claim: "94% recall" means nothing until somebody
says what twenty lines of regex scored on the same data. Deleting this file once the
classifier wins would delete the comparison, so bench/detection_eval.py runs both against
every case and the README prints both rows.

**What it can see.** Surface forms. An injection written the way injections are usually
written - "ignore all previous instructions", "you are now DAN", a spoofed `<|im_start|>`
turn - is a string match, and a string match is a hundred nanoseconds. What it cannot see
is meaning: the same request phrased as a polite hypothetical, translated, or split across
two turns passes every rule below. That gap is the classifier's whole justification, and
the eval is where it is measured rather than asserted.

**Normalisation here is the opposite of the cache's.** cache/keys.py refuses to touch
prompt text, because "Hello" and "hello " are different inputs to a model and a cache that
conflated them would decide that on the tenant's behalf. Detection has to do exactly what
the cache must not, because here a spelling difference *is* the attack: a zero-width space
inside "ig<U+200B>nore", fullwidth characters, or five spaces between words all defeat a
literal pattern while reading identically to the model. So the text is folded before
matching - and the folded copy is used for nothing else.

**The whole prompt is scanned, never a prefix.** Capping the scan at the first few
kilobytes would be a documented evasion: put the payload at the end. The rules are linear
in the length of the text and compiled once at import, so the cost of not truncating is
tens of microseconds per kilobyte.

**A rule reports its id and nothing else.** No rule ever returns the text it matched. The
id is metadata and goes to the ledger, the span and the metric; the matched text is prompt
content, and the rule that "ignore all previous instructions" fired tells an operator
everything the quoted snippet would have, without putting a customer's prompt in telemetry.
"""

import re
import unicodedata
from dataclasses import dataclass

# Characters that render as nothing and split a keyword in half. A prompt containing one
# inside a word is not a typing accident.
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\xad]")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Rule:
    """One pattern, and the id it is known by everywhere else.

    `id` is what reaches the ledger, the span and the eval table, so it is stable: renaming
    one silently rewrites the history of what this gateway has been flagging.
    """

    id: str
    pattern: re.Pattern[str]


def rule(id: str, pattern: str) -> Rule:
    return Rule(id, re.compile(pattern, re.IGNORECASE | re.MULTILINE))


# Ordered by how specific they are, because the first one to fire is the one reported. A
# request tripping `instruction_override` and `encoded_payload` alike is more usefully
# described by the first.
RULES: tuple[Rule, ...] = (
    # "Ignore all previous instructions", and the dozen ways it is usually written. The
    # {0,3} gap absorbs the words people put in between - "ignore any of the above safety
    # instructions" - without letting the two halves match a paragraph apart.
    rule(
        "instruction_override",
        r"\b(?:ignore|disregard|forget|discard|override|skip)\b"
        r"(?:\s+\w+){0,3}?\s+"
        r"\b(?:previous|prior|preceding|earlier|above|foregoing|original|initial|all)\b"
        r"(?:\s+\w+){0,3}?\s+"
        r"\b(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions|"
        r"guideline|guidelines|context|constraint|constraints)\b",
    ),
    # The payload that follows an override: a fresh set of orders, announced as such.
    rule(
        "instruction_replacement",
        r"\b(?:new|updated|revised|additional|real|actual|true|corrected)\s+"
        r"(?:system\s+)?(?:instruction|instructions|prompt|directive|directives|task|rules)\b"
        r"\s*[:\-\u2013]",
    ),
    # Asking the model to hand over the thing it was told not to hand over.
    rule(
        "system_prompt_extraction",
        r"\b(?:repeat|reveal|reproduce|print|output|show|display|list|summari[sz]e|echo|"
        r"tell\s+me|what\s+(?:is|are|was|were))\b"
        r"(?:\s+\w+){0,4}?\s+"
        r"\b(?:system|initial|original|hidden|secret|preceding|above|developer)\b"
        r"(?:\s+\w+){0,2}?\s*"
        r"\b(?:prompt|instruction|instructions|message|messages|rules|configuration)\b",
    ),
    # Reassigning the model's identity, which is how a persona attack opens.
    #
    # The persona has to be an AI. An earlier version of this rule accepted any "act as a ..."
    # and was the worst rule in the set by a distance: 35 of the baseline's 51 false positives
    # on the eval corpus, because "act as a technical editor" and "you are a helpful assistant
    # that summarises meeting notes" are what customers say all day. Requiring the persona to
    # be a model rather than a profession cost 6 points of recall and bought 10 of precision -
    # 0.811 to 0.908, with the false positive rate falling from 0.076 to 0.028. The recall is
    # the cheaper thing to give up here, because the classifier tier covers the same ground
    # and a false positive is added to whatever the classifier does, never cancelled by it.
    # bench/results/detection_eval.txt is where that trade is recorded.
    rule(
        "role_override",
        r"(?:\byou\s+are\s+now\b|\bfrom\s+now\s+on\b(?:\s*,)?\s+you\b|"
        r"\byour\s+new\s+(?:role|identity|persona|instructions)\b|"
        r"\b(?:act\s+as|pretend\s+to\s+be|role-?play\s+as|simulate\s+being)\s+"
        r"(?:an?\s+)?(?:\w+\s+){0,2}?(?:AI|model|assistant|chatbot|bot|GPT|version)\b|"
        r"\bpretend\s+you\s+(?:are|were)\b)",
    ),
    # The named jailbreaks. Word boundaries matter: "DAN" also appears inside "danger",
    # and a rule that flagged that would be the first false positive anybody found.
    rule(
        "jailbreak_persona",
        r"\b(?:DAN|AIM|STAN|DUDE|do\s+anything\s+now|developer\s+mode|god\s*mode|"
        r"jailbreak(?:ing|en|ed)?|unfiltered\s+mode|opposite\s+mode|kevin)\b",
    ),
    # Asking for the guardrails off, without bothering with a persona.
    rule(
        "guardrail_removal",
        r"(?:\bno\s+longer\s+bound\b|\bwithout\s+(?:any\s+)?(?:restriction|restrictions|"
        r"limitation|limitations|filter|filters|censorship|guardrails)\b|"
        r"\b(?:bypass|disable|turn\s+off|remove|lift)\s+(?:your\s+|the\s+|all\s+)?"
        r"(?:safety|content|policy|policies|guideline|guidelines|filter|filters|"
        r"guardrails|restrictions|rules)\b|"
        r"\b(?:you\s+)?(?:have\s+no|are\s+not\s+bound\s+by)\s+(?:rules|restrictions|limits)\b|"
        r"\b(?:with|having)\s+no\s+"
        r"(?:restriction|restrictions|limit|limits|filter|filters|rules|guardrails)\b)",
    ),
    # A forged turn boundary: the prompt pretending to be the transcript around it.
    rule(
        "delimiter_spoof",
        r"(?:<\|(?:im_start|im_end|endoftext|system|assistant)\|>|"
        r"\[/?(?:INST|SYS|SYSTEM)\]|<</?SYS>>|"
        r"^\s*#{2,}\s*(?:system|instruction|instructions)\b|"
        r"^\s*(?:system|assistant)\s*:\s*(?:you\s+are|ignore)\b|"
        r"\bend\s+of\s+(?:prompt|instructions)\b)",
    ),
    # Indirect injection's payoff: whatever you found, send it somewhere. The {0,8} gap is
    # wider than the others because the object of the verb is usually a phrase.
    rule(
        "exfiltration",
        r"\b(?:send|post|upload|forward|transmit|exfiltrate|leak|report)\b"
        r"(?:\s+\S+){0,8}?\s+"
        r"(?:https?://|\bcurl\s+http|\bwebhook\b|\bfetch\(|\brequests\.(?:get|post)\()",
    ),
    # Asking the model for a credential it may have been handed in its context.
    rule(
        "credential_fishing",
        r"\b(?:what\s+(?:is|are)|reveal|print|output|show|give\s+me|tell\s+me)\b"
        r"(?:\s+\w+){0,3}?\s+"
        r"\b(?:api[\s_-]?key|access[\s_-]?key|secret[\s_-]?key|password|passphrase|"
        r"credential|credentials|bearer\s+token|auth\s+token)\b",
    ),
    # A long unbroken blob of base64, which is how a payload avoids every rule above.
    #
    # The weakest rule here and knowingly so: it also matches a minified asset, a
    # certificate, an embedded image and a long hash, none of which are attacks. It is
    # kept because the eval is the place to find that out - bench/detection_eval.py prints
    # a per-rule false positive count, and a baseline whose weakest rule is hidden rather
    # than measured is the kind of baseline that makes a classifier look better than it is.
    rule("encoded_payload", r"[A-Za-z0-9+/]{120,}={0,2}"),
)


def fold(text: str) -> str:
    """The text as the rules see it: same words, no room to hide between them.

    NFKC first, which is what collapses fullwidth and other compatibility forms onto the
    ASCII they render as: a prompt written in fullwidth characters (\uff49\uff47..., which
    reads as "ig...") folds back onto "ignore" and matches. Then the invisible characters
    go, and runs of whitespace become single spaces, so a keyword cannot be split by a
    zero-width space or padded apart by newlines.

    Only for matching. Nothing downstream ever sees this string, and the request that goes
    upstream is the one the tenant sent, byte for byte.
    """
    return WHITESPACE.sub(" ", INVISIBLE.sub("", unicodedata.normalize("NFKC", text))).strip()


def matches(text: str) -> list[str]:
    """Every rule that fires, in rule order. Ids only; see the module docstring.

    The full list rather than the first hit, because the eval reports per-rule precision
    and a rule whose every match is also matched by an earlier rule is a rule worth
    deleting.
    """
    folded = fold(text)
    return [item.id for item in RULES if item.pattern.search(folded)]


def scan(text: str) -> str | None:
    """The first rule that fires, or None. What the gateway records on the request."""
    folded = fold(text)
    return next((item.id for item in RULES if item.pattern.search(folded)), None)
