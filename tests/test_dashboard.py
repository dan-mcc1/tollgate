"""The dashboard and the alert, checked against the code they describe.

A dashboard is the one artefact that fails silently: rename an instrument and every panel
goes blank, with nothing failing anywhere until someone opens it during an incident and
finds it empty. These tests are what makes that a build failure instead.
"""

import hashlib
import json
import re
from typing import Any

import pytest

from dashboards import manifest
from tests.conftest import ROOT
from tollgate.telemetry import INSTRUMENT_NAMES, POLICY_REFUSALS, outcome_for

DASHBOARD = ROOT / "dashboards" / "tollgate.json"
ALERT = ROOT / "infra" / "grafana" / "main.tf"

# Suffixes Prometheus appends: the unit, and the shape of the instrument.
SUFFIXES = ("_bucket", "_sum", "_count", "_total", "_milliseconds")
METRIC_PATTERN = re.compile(r"\btollgate_[a-z_]+\b")
PNG_MAGIC = bytes([0x89]) + b"PNG"

# Every outcome `outcome_for` can produce. Written out rather than derived, so adding one
# to the code is a decision about the dashboard too rather than a silent default colour.
OUTCOMES = {
    "ok",
    "upstream_error_in_stream",
    "unauthenticated",
    "budget_exhausted",
    "rate_limited",
    "unsupported",
    "prompt_blocked",
    "response_blocked",
    "upstream_error",
    "gateway_error",
}


@pytest.fixture(scope="module")
def dashboard() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return loaded


def base_name(prometheus_name: str) -> str:
    """`tollgate_overhead_milliseconds_bucket` back to `tollgate.overhead`."""
    name = prometheus_name
    changed = True
    while changed:
        changed = False
        for suffix in SUFFIXES:
            if name.endswith(suffix):
                name, changed = name[: -len(suffix)], True
    return name.replace("_", ".")


def queries(dashboard: dict[str, Any]) -> list[str]:
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]


# --- the dashboard describes metrics that exist ------------------------------------------


def test_every_metric_the_dashboard_queries_is_one_the_gateway_emits(
    dashboard: dict[str, Any],
) -> None:
    known = set(INSTRUMENT_NAMES)
    referenced = {
        base_name(match) for query in queries(dashboard) for match in METRIC_PATTERN.findall(query)
    }

    assert referenced, "no metrics found in the dashboard; the parser is probably wrong"
    assert referenced <= known, f"dashboard queries metrics that do not exist: {referenced - known}"


def test_the_alert_queries_a_metric_that_exists() -> None:
    """The alert lives in Terraform rather than the dashboard JSON, so it would survive
    a rename that the test above caught."""
    referenced = {
        base_name(match) for match in METRIC_PATTERN.findall(ALERT.read_text(encoding="utf-8"))
    }

    assert referenced, "no metric found in the alert rule"
    assert referenced <= INSTRUMENT_NAMES


def test_the_alert_watches_overhead_rather_than_total_latency() -> None:
    """Total latency is mostly the provider generating tokens, which this service cannot
    control. An alert on that fires whenever the model has a slow afternoon and is muted
    within a week."""
    rule = ALERT.read_text(encoding="utf-8")

    assert "tollgate_overhead_milliseconds_bucket" in rule
    assert "tollgate_request_duration" not in rule


# --- the dashboard covers what the code can produce ----------------------------------------


def test_outcome_for_produces_exactly_the_outcomes_the_dashboard_colours(
    dashboard: dict[str, Any],
) -> None:
    """Both directions. A new outcome in the code with no colour in the dashboard gets
    whatever Grafana hands out next, which moves as series come and go; a colour with no
    outcome is a panel legend entry that never appears."""
    produced = {outcome_for(code, None) for code in (200, 401, 402, 429, 500, 502, 501)}
    # The two policy refusals share a status, so the code is what tells them apart.
    produced |= {outcome_for(403, None, code) for code in POLICY_REFUSALS}
    produced.add(outcome_for(200, "upstream"))
    produced.add(outcome_for(500, "upstream"))

    panel = next(p for p in dashboard["panels"] if p["title"] == "Requests by outcome")
    coloured = {override["matcher"]["options"] for override in panel["fieldConfig"]["overrides"]}

    assert produced == OUTCOMES
    assert coloured == OUTCOMES


def test_outcome_colours_are_pinned_to_the_outcome_not_the_series_order(
    dashboard: dict[str, Any],
) -> None:
    """Grafana assigns palette colours by series order, so filtering one outcome away
    repaints the rest and "the red one" means something different from one minute to the
    next. A fixed colour per outcome is what stops that."""
    panel = next(p for p in dashboard["panels"] if p["title"] == "Requests by outcome")

    for override in panel["fieldConfig"]["overrides"]:
        [colour] = [p for p in override["properties"] if p["id"] == "color"]
        assert colour["value"]["mode"] == "fixed"
        assert colour["value"]["fixedColor"]


# --- it stays readable and portable ----------------------------------------------------------


def test_no_panel_is_pinned_to_one_grafana_stack(dashboard: dict[str, Any]) -> None:
    """Panels reference the data source variable, so the same JSON works against a local
    Prometheus and against Grafana Cloud without editing."""
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] == "${datasource}", panel["title"]


def test_multi_series_panels_carry_a_legend(dashboard: dict[str, Any]) -> None:
    """Identity is never colour alone."""
    for panel in dashboard["panels"]:
        if panel["type"] != "timeseries":
            continue
        targets = panel.get("targets", [])
        grouped = any("{{" in target.get("legendFormat", "") for target in targets)
        if len(targets) > 1 or grouped:
            assert panel["options"]["legend"]["showLegend"] is True, panel["title"]


def test_latency_and_overhead_are_not_drawn_on_one_axis(dashboard: dict[str, Any]) -> None:
    """Overhead is milliseconds and a model call is seconds. Sharing an axis flattens
    the smaller one onto the baseline, which is the most common way a chart lies."""
    titles = [panel["title"] for panel in dashboard["panels"]]

    assert "Gateway overhead" in titles
    assert "Upstream latency" in titles
    for title in ("Gateway overhead", "Upstream latency"):
        panel = next(p for p in dashboard["panels"] if p["title"] == title)
        units = {target.get("expr", "") for target in panel["targets"]}
        assert all("overhead" in expr or "upstream_duration" in expr for expr in units)


def test_every_panel_says_what_it_is_for(dashboard: dict[str, Any]) -> None:
    """A panel whose meaning lives only in the head of whoever made it is a panel that
    gets misread at three in the morning."""
    undocumented = [
        panel["title"]
        for panel in dashboard["panels"]
        if panel["title"] not in {"Requests / min"} and not panel.get("description")
    ]

    assert undocumented == []


def test_the_dashboard_file_is_what_terraform_applies() -> None:
    """Terraform reads the file rather than embedding a copy, so the version in the
    repository is the version in Grafana and a pull request shows the real diff."""
    assert 'file("${path.module}/../../dashboards/tollgate.json")' in ALERT.read_text(
        encoding="utf-8"
    )


RETAKE = (
    "Retake the screenshots from the live dashboard, then record them:\n"
    "    uv run python dashboards/manifest.py"
)


def test_the_committed_screenshots_match_the_committed_dashboard() -> None:
    """The README shows pictures of this dashboard, and a picture is the one artefact
    that cannot be regenerated from the repository. If the JSON has been edited since
    they were taken, the page shows a dashboard that no longer exists.

    The link is recorded rather than inferred from file times, because git does not store
    modification times: on a fresh checkout every file is written within the same millisecond,
    in whatever order the checkout happens to use.
    """
    recorded = json.loads((DASHBOARD.parent / "screenshots.json").read_text(encoding="utf-8"))

    assert recorded["screenshots"], f"no screenshots recorded. {RETAKE}"
    assert recorded["dashboard_sha256"] == manifest.dashboard_digest(), (
        f"dashboards/{DASHBOARD.name} has changed since the screenshots were taken. {RETAKE}"
    )

    for name in recorded["screenshots"]:
        screenshot = DASHBOARD.parent / name
        assert screenshot.exists(), f"{name} is recorded but missing. {RETAKE}"
        assert screenshot.read_bytes().startswith(PNG_MAGIC), f"{name} is not a PNG"


def test_the_recorded_hash_does_not_depend_on_line_endings() -> None:
    """The reason the digest parses the file instead of hashing its bytes.

    `.gitattributes` sets `eol=lf`, so the repository stores LF and a Windows checkout
    gets CRLF. A byte hash therefore answers differently on the machine that took the
    screenshots than on the runner that checks them - which is a test that passes
    locally and fails in CI permanently, and did.
    """
    lf = DASHBOARD.read_text(encoding="utf-8").replace("\r\n", "\n")
    crlf = lf.replace("\n", "\r\n")

    def digest_of(text: str) -> str:
        content = json.loads(text)
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert digest_of(lf) == digest_of(crlf) == manifest.dashboard_digest()


def test_no_screenshot_is_missing_from_the_manifest() -> None:
    """A picture nobody recorded is one that never goes stale, which is the same as not
    being checked at all."""
    recorded = json.loads((DASHBOARD.parent / "screenshots.json").read_text(encoding="utf-8"))

    assert sorted(recorded["screenshots"]) == manifest.screenshots(), (
        f"the folder and the manifest disagree about which screenshots exist. {RETAKE}"
    )


def test_every_screenshot_is_shown_in_the_prose() -> None:
    """A screenshot nobody references is one nobody notices going stale, and the test
    above would keep insisting it be retaken forever.

    The README and everything under docs/ count, because they are one document as far as a
    reader is concerned: the README carries the headline screen and docs/operations.md the
    rest. Checking only the README would let a screenshot referenced from the docs alone
    fail this, and checking only that a file exists somewhere would miss the point.
    """
    pages = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    prose = "\n".join(page.read_text(encoding="utf-8") for page in pages)

    for screenshot in sorted(DASHBOARD.parent.glob("*.png")):
        assert f"dashboards/{screenshot.name}" in prose, f"{screenshot.name} is unreferenced"
