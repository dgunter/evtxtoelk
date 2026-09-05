#!/usr/bin/env python3
"""Post the SonarCloud pull-request analysis back onto the GitHub PR.

Runs right after the SonarQube Scan step, on pull_request events only:

  1. read .scannerwork/report-task.txt to find the compute-engine task,
  2. wait for SonarCloud to finish processing it,
  3. fetch the PR's quality-gate status and every open issue on its new code,
  4. upsert ONE sticky comment on the PR (marker-based; edited in place on
     every push, so the PR conversation holds exactly one current report
     that a reviewer or an agent can read without leaving GitHub),
  5. emit workflow annotations so issues show inline in "Files changed",
  6. mirror the report into the job summary.

Stdlib only, so it runs on the runner's system python3.

Environment:
  SONAR_TOKEN            SonarCloud token (same one the scan step used)
  GITHUB_TOKEN           needs `pull-requests: write` to comment
  GITHUB_REPOSITORY      owner/repo (set by Actions)
  PR_NUMBER              pull request number
  PR_HEAD_SHA            head commit, shown in the report footer
  GITHUB_STEP_SUMMARY    optional, set by Actions
  SONAR_REPORT_TASK      optional override of the report-task.txt path
  SONAR_GATE_FAILS_JOB   "true" to exit non-zero on a red quality gate
                         (default: report only, never block)
  SONAR_DRY_RUN          "true" to print the comment instead of posting it
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MARKER = "<!-- sonarcloud-pr-report -->"
COMMENT_LIMIT = 60_000  # GitHub caps issue comments at 65,536 chars
TABLE_ROW_CAP = 50  # issue rows shown in the comment table
ANNOTATION_CAP = 10  # GitHub renders at most 10 per level per step
SEVERITY_ORDER = {"BLOCKER": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
PERCENT_METRICS = {
    "new_coverage",
    "new_duplicated_lines_density",
    "new_security_hotspots_reviewed",
    "coverage",
    "duplicated_lines_density",
}
METRIC_LABELS = {
    "new_reliability_rating": "Reliability rating (new code)",
    "new_security_rating": "Security rating (new code)",
    "new_maintainability_rating": "Maintainability rating (new code)",
    "new_coverage": "Coverage (new code)",
    "new_duplicated_lines_density": "Duplication (new code)",
    "new_security_hotspots_reviewed": "Security hotspots reviewed",
    "new_violations": "New issues",
}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, headers: dict[str, str], body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (https only)
        raw = resp.read()
    return json.loads(raw) if raw else None


def sonar_get(server: str, token: str, path: str, **params: Any) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{server.rstrip('/')}/{path}?{query}"
    return _request("GET", url, {"Authorization": f"Bearer {token}"})


def github(method: str, token: str, path: str, body: Any = None) -> Any:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return _request(method, f"https://api.github.com{path}", headers, body)


# --------------------------------------------------------------------------- #
# SonarCloud
# --------------------------------------------------------------------------- #
def read_report_task(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        pairs = (line.strip().split("=", 1) for line in fh if "=" in line)
        return {k: v for k, v in pairs}


def wait_for_task(server: str, token: str, task_id: str, timeout: int = 300) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        task = sonar_get(server, token, "api/ce/task", id=task_id)["task"]
        status = task.get("status")
        if status == "SUCCESS":
            return task
        if status in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"SonarCloud task {task_id} ended with status {status}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"SonarCloud task {task_id} still {status} after {timeout}s")
        time.sleep(5)


def fetch_gate(server: str, token: str, project: str, pr: str) -> dict[str, Any]:
    return sonar_get(
        server, token, "api/qualitygates/project_status", projectKey=project, pullRequest=pr
    )["projectStatus"]


def fetch_issues(server: str, token: str, project: str, pr: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        data = sonar_get(
            server,
            token,
            "api/issues/search",
            componentKeys=project,
            pullRequest=pr,
            resolved="false",
            ps=500,
            p=page,
        )
        issues.extend(data.get("issues", []))
        paging = data.get("paging", {})
        if page * paging.get("pageSize", 500) >= paging.get("total", 0) or page >= 20:
            return issues
        page += 1


def normalize(issue: dict[str, Any], project: str) -> dict[str, Any]:
    impacts = issue.get("impacts") or []
    severity = max(
        (i.get("severity", "LOW") for i in impacts),
        key=lambda s: -SEVERITY_ORDER.get(s, 9),
        default=issue.get("severity", "LOW"),
    )
    qualities = sorted({i.get("softwareQuality", "").title() for i in impacts} - {""})
    component = issue.get("component", "")
    path = component.split(":", 1)[1] if component.startswith(f"{project}:") else component
    return {
        "key": issue.get("key"),
        "severity": severity,
        "qualities": qualities or [issue.get("type", "").replace("_", " ").title()],
        "path": path,
        "line": issue.get("line"),
        "message": issue.get("message", ""),
        "rule": issue.get("rule", ""),
        "effort": issue.get("effort", ""),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_value(metric: str, value: str | None) -> str:
    if value is None or value == "":
        return "—"
    if metric.endswith("_rating"):
        return {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}.get(value, value)
    if metric in PERCENT_METRICS:
        return f"{float(value):.1f}%"
    return value


def _fmt_threshold(cond: dict[str, Any]) -> str:
    metric = cond.get("metricKey", "")
    threshold = _fmt_value(metric, cond.get("errorThreshold"))
    op = {"GT": "≤", "LT": "≥"}.get(cond.get("comparator", ""), "")
    return f"{op} {threshold}".strip()


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(
    *,
    pr: str,
    project: str,
    org: str,
    server: str,
    gate: dict[str, Any],
    issues: list[dict[str, Any]],
    head_sha: str,
    task_url: str,
) -> str:
    status = gate.get("status", "NONE")
    icon = {"OK": "✅ Passed", "ERROR": "❌ Failed", "WARN": "⚠️ Warning"}.get(
        status, f"⚪ {status}"
    )
    dashboard = f"{server}/dashboard?id={urllib.parse.quote(project)}&pullRequest={pr}"
    lines = [
        MARKER,
        f"## SonarCloud analysis · PR #{pr}",
        "",
        f"**Quality gate: {icon}** · [Open in SonarCloud]({dashboard})",
        "",
        "| Condition | Actual | Required | Status |",
        "|---|---|---|---|",
    ]
    for cond in gate.get("conditions", []):
        metric = cond.get("metricKey", "")
        ok = cond.get("status") == "OK"
        lines.append(
            f"| {METRIC_LABELS.get(metric, metric)} | {_fmt_value(metric, cond.get('actualValue'))} "
            f"| {_fmt_threshold(cond)} | {'✅' if ok else '❌'} |"
        )

    lines += ["", f"### Open issues on new code: {len(issues)}", ""]
    if not issues:
        lines.append("No open issues on the changed code. 🎉")
    else:
        lines += [
            "| Severity | Quality | Location | Message | Rule |",
            "|---|---|---|---|---|",
        ]
        for issue in issues[:TABLE_ROW_CAP]:
            loc = f"`{issue['path']}:{issue['line']}`" if issue["line"] else f"`{issue['path']}`"
            rule = issue["rule"]
            rule_url = (
                f"{server}/organizations/{org}/rules?open={urllib.parse.quote(rule)}"
                f"&rule_key={urllib.parse.quote(rule)}"
            )
            lines.append(
                f"| {issue['severity']} | {', '.join(issue['qualities'])} | {loc} "
                f"| {_md_cell(issue['message'])} | [{rule}]({rule_url}) |"
            )
        if len(issues) > TABLE_ROW_CAP:
            lines.append(f"| … | | | {len(issues) - TABLE_ROW_CAP} more in SonarCloud | |")

        lines += [
            "",
            "<details><summary>Machine-readable issue list</summary>",
            "",
            "```json",
            json.dumps(issues, indent=1),
            "```",
            "",
            "</details>",
        ]

    lines += [
        "",
        f"_Commit `{head_sha[:12]}` · [analysis task]({task_url}) · "
        "posted by the Build workflow; edited in place on every push._",
    ]
    body = "\n".join(lines)
    if len(body) > COMMENT_LIMIT:
        cut = body.rfind("\n", 0, COMMENT_LIMIT - 200)
        body = body[:cut] + "\n\n_Report truncated; open SonarCloud for the full list._\n"
    return body


def emit_annotations(issues: list[dict[str, Any]]) -> None:
    """GitHub workflow commands: show up inline in the Files changed tab."""

    def esc(text: str, prop: bool = False) -> str:
        text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        if prop:
            text = text.replace(":", "%3A").replace(",", "%2C")
        return text

    counts = {"error": 0, "warning": 0}
    for issue in issues:
        level = "error" if issue["severity"] in {"BLOCKER", "HIGH"} else "warning"
        if counts[level] >= ANNOTATION_CAP:
            continue
        counts[level] += 1
        props = f"file={esc(issue['path'], True)}"
        if issue["line"]:
            props += f",line={issue['line']}"
        props += f",title={esc('SonarCloud ' + issue['rule'], True)}"
        print(f"::{level} {props}::{esc(issue['message'])}")
    hidden = len(issues) - sum(counts.values())
    if hidden > 0:
        print(
            f"::notice title=SonarCloud::{hidden} more issue(s) not annotated "
            "(10-per-level cap); see the PR comment."
        )


# --------------------------------------------------------------------------- #
# GitHub comment upsert
# --------------------------------------------------------------------------- #
def upsert_comment(repo: str, pr: str, token: str, body: str) -> str:
    page = 1
    while True:
        comments = github(
            "GET", token, f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}"
        )
        for comment in comments:
            if MARKER in (comment.get("body") or ""):
                github(
                    "PATCH",
                    token,
                    f"/repos/{repo}/issues/comments/{comment['id']}",
                    {"body": body},
                )
                return f"updated comment {comment['id']}"
        if len(comments) < 100:
            break
        page += 1
    created = github("POST", token, f"/repos/{repo}/issues/{pr}/comments", {"body": body})
    return f"created comment {created['id']}"


# --------------------------------------------------------------------------- #
def main() -> int:
    env = os.environ
    sonar_token = env.get("SONAR_TOKEN", "")
    pr = env.get("PR_NUMBER", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    if not (sonar_token and pr):
        print("::notice title=SonarCloud::SONAR_TOKEN or PR_NUMBER missing; skipping PR report.")
        return 0

    task_file = env.get("SONAR_REPORT_TASK", ".scannerwork/report-task.txt")
    meta = read_report_task(task_file)
    server = meta.get("serverUrl", "https://sonarcloud.io")
    project = meta["projectKey"]
    org = meta.get("organization") or env.get("SONAR_ORGANIZATION") or project.split("_", 1)[0]

    print(f"Waiting for SonarCloud to process task {meta['ceTaskId']} …")
    wait_for_task(server, sonar_token, meta["ceTaskId"])
    gate = fetch_gate(server, sonar_token, project, pr)
    issues = sorted(
        (normalize(i, project) for i in fetch_issues(server, sonar_token, project, pr)),
        key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), i["path"], i["line"] or 0),
    )
    print(f"Quality gate {gate.get('status')} · {len(issues)} open issue(s) on new code")

    body = render_markdown(
        pr=pr,
        project=project,
        org=org,
        server=server,
        gate=gate,
        issues=issues,
        head_sha=env.get("PR_HEAD_SHA", ""),
        task_url=meta.get("ceTaskUrl", server),
    )
    emit_annotations(issues)

    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body.replace(MARKER, "").lstrip() + "\n")

    if env.get("SONAR_DRY_RUN", "").lower() == "true":
        print(body)
    else:
        try:
            print(upsert_comment(repo, pr, env.get("GITHUB_TOKEN", ""), body))
        except urllib.error.HTTPError as exc:  # fork PRs get a read-only token
            print(
                f"::warning title=SonarCloud::Could not post PR comment (HTTP {exc.code}); "
                "the report is in the job summary instead."
            )

    if gate.get("status") == "ERROR" and env.get("SONAR_GATE_FAILS_JOB", "").lower() == "true":
        print("::error title=SonarCloud::Quality gate failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
