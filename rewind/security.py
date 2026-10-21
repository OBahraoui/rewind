"""Security findings an agent introduced, read from the pipeline's scanner reports.

GitLab's analyzers write standard JSON reports as job artifacts. Rewind reads them
straight from the MR pipeline (no Ultimate-only security APIs needed) and keeps only
findings in files the change touched, so agents fix what they introduced and are not
asked to fix the whole backlog.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("rewind.security")

# job name pattern -> report artifact path
REPORTS = [
    (re.compile(r"sast"), "gl-sast-report.json"),
    (re.compile(r"dependency[-_]scanning"), "gl-dependency-scanning-report.json"),
    (re.compile(r"secret[-_]detection"), "gl-secret-detection-report.json"),
]


def _finding(v: dict[str, Any], scanner: str) -> dict[str, Any]:
    loc = v.get("location") or {}
    dep = (loc.get("dependency") or {}).get("package", {}).get("name")
    ident = next((i.get("value") or i.get("name") for i in v.get("identifiers") or []), "")
    return {
        "scanner": scanner,
        "severity": v.get("severity") or "Unknown",
        "name": v.get("name") or v.get("message") or ident or "finding",
        "file": loc.get("file") or "",
        "line": loc.get("start_line"),
        "dependency": dep,
        "identifier": ident,
    }


def introduced_findings(gitlab: Any, pipeline_id: int, changed_paths: set[str],
                        severities: list[str]) -> list[dict[str, Any]]:
    wanted = {s.lower() for s in severities}
    out: dict[tuple, dict[str, Any]] = {}
    for job in gitlab.list_pipeline_jobs(pipeline_id):
        if job.get("status") != "success" or "id" not in job:
            continue
        for pattern, path in REPORTS:
            if not pattern.search(job["name"]):
                continue
            raw = gitlab.job_artifact(job["id"], path)
            if not raw:
                continue
            try:
                report = json.loads(raw)
            except ValueError:
                log.warning("unreadable security report", extra={"job": job["name"]})
                continue
            for v in report.get("vulnerabilities") or []:
                f = _finding(v, job["name"])
                if f["severity"].lower() not in wanted or f["file"] not in changed_paths:
                    continue
                out[(f["file"], f["line"], f["name"])] = f
    return sorted(out.values(), key=lambda f: (f["file"], f["line"] or 0))


def describe(f: dict[str, Any]) -> str:
    where = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
    what = f" ({f['dependency']})" if f.get("dependency") else ""
    return f"[{f['severity']}] {f['name']}{what} at {where}"
