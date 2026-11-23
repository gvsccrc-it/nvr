"""Fail on a known vulnerability in the locked Python packages, the web interface's packages, or the image's system packages (#385, #548).

Python: pip-audit over requirements.txt, the hashed lock the image installs (run it with the pip-audit from requirements-ci-audit.txt).
Web: npm audit over web/package-lock.json.
Image: ``--image REPORT``, a Trivy JSON report of the built image's system packages (Debian, FFmpeg above all). A High or Critical finding
with a fix available fails; one with no fix yet is reported, not failed (the owner's decision on #548).

A finding listed in .github/vulnerability-exceptions.txt is reported but accepted. Each line there gives the advisory's ID, the date the
exception expires and the reason; a line without a reason or a date, a date already past, or one more than ``MAX_DAYS`` ahead is refused,
so an exception is looked at again at least monthly. Prints what it found; exits 1 if anything not accepted is left, or if a check could
not run."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCEPTIONS = ROOT / ".github" / "vulnerability-exceptions.txt"
LINE = re.compile(r"^(?P<id>[A-Za-z]+-[\w-]+)\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<reason>\S.*)$")
MAX_DAYS = 30
FAILING = {"HIGH", "CRITICAL"}


def accepted(today: date | None = None) -> dict[str, str]:
    today = today or date.today()
    found: dict[str, str] = {}
    for number, raw in enumerate(EXCEPTIONS.read_text(encoding="utf-8").splitlines(), 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        match = LINE.match(text)
        if not match:
            raise SystemExit(f"{EXCEPTIONS.name}:{number}: give the advisory ID, the date it expires and a reason: {text!r}")
        try:
            expires = date.fromisoformat(match["date"])
        except ValueError:
            raise SystemExit(f"{EXCEPTIONS.name}:{number}: {match['date']} is not a date") from None
        if expires < today:
            raise SystemExit(f"{EXCEPTIONS.name}:{number}: the exception for {match['id']} expired on {expires}: look at it again, and remove it or renew it")
        if expires > today + timedelta(days=MAX_DAYS):
            raise SystemExit(f"{EXCEPTIONS.name}:{number}: the exception for {match['id']} runs to {expires}, more than {MAX_DAYS} days ahead")
        found[match["id"].upper()] = match["reason"]
    return found


def python_findings(pip_audit: str) -> list[tuple[str, set[str], str]]:
    result = subprocess.run([pip_audit, "-r", str(ROOT / "requirements.txt"), "--disable-pip", "--no-deps", "--progress-spinner", "off", "-f", "json"],
                            capture_output=True, text=True, timeout=600)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"pip-audit did not run:\n{result.stderr[-2000:]}") from None
    return [(f"{package['name']} {package['version']}", {vuln["id"].upper(), *(alias.upper() for alias in vuln.get("aliases", []))}, vuln.get("description", "")[:160])
            for package in report.get("dependencies", []) for vuln in package.get("vulns", [])]


def web_findings() -> list[tuple[str, set[str], str]]:
    result = subprocess.run(["npm", "audit", "--json"], cwd=ROOT / "web", capture_output=True, text=True, timeout=300)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"npm audit did not run:\n{result.stderr[-2000:]}") from None
    if "error" in report:
        raise SystemExit(f"npm audit did not run: {report['error']}")
    found = []
    for name, entry in report.get("vulnerabilities", {}).items():
        for via in entry.get("via", []):
            if isinstance(via, dict):  # A string names another package that is itself listed
                advisory = str(via.get("url", "")).rsplit("/", 1)[-1].upper()
                found.append((f"{name} {entry.get('range', '')}".strip(), {advisory} if advisory else set(), str(via.get("title", ""))[:160]))
    return found


def image_findings(report_path: Path) -> tuple[list[tuple[str, set[str], str]], list[tuple[str, set[str], str]]]:
    """The image's High and Critical findings from a Trivy JSON report: (those with a fix available, those with none yet)."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"The image scan did not run: {exc}") from None
    if not isinstance(report, dict) or "Results" not in report:
        raise SystemExit("The image scan did not run: the report has no results")
    fixable, waiting = [], []
    for result in report.get("Results") or []:
        for vuln in result.get("Vulnerabilities") or []:
            if str(vuln.get("Severity", "")).upper() not in FAILING:
                continue
            ids = {str(vuln.get("VulnerabilityID", "")).upper()} - {""}
            package = f"{vuln.get('PkgName', '?')} {vuln.get('InstalledVersion', '')}".strip()
            fixed = str(vuln.get("FixedVersion") or "").strip()
            title = f"{vuln.get('Severity')}: {str(vuln.get('Title') or '')[:120]}" + (f" (fixed in {fixed})" if fixed else "")
            (fixable if fixed else waiting).append((package, ids, title))
    return fixable, waiting


def judge(findings: list[tuple[str, str, set[str], str]], allowed: dict[str, str]) -> int:
    left = 0
    for where, package, ids, title in findings:
        reason = next((allowed[item] for item in ids if item in allowed), None)
        label = ", ".join(sorted(ids)) or "no advisory ID"
        if reason:
            print(f"accepted  {where}: {package}: {label}: {reason}")
        else:
            left += 1
            print(f"VULNERABLE {where}: {package}: {label}: {title}")
    return left


def main() -> int:
    allowed = accepted()
    if len(sys.argv) > 2 and sys.argv[1] == "--image":
        fixable, waiting = image_findings(Path(sys.argv[2]))
        left = judge([("image", *item) for item in fixable], allowed)
        for package, ids, title in waiting:
            print(f"no fix yet image: {package}: {', '.join(sorted(ids))}: {title}")
        print(f"{len(fixable)} High or Critical findings in the image's system packages with a fix available, {left} not accepted; "
              f"{len(waiting)} with no fix yet (reported, not failed).")
        return 1 if left else 0
    pip_audit = sys.argv[1] if len(sys.argv) > 1 else "pip-audit"
    findings = [("python", *item) for item in python_findings(pip_audit)] + [("web", *item) for item in web_findings()]
    left = judge(findings, allowed)
    print(f"{len(findings)} known vulnerabilities, {left} not accepted.")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
