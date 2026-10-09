"""Compact, plain-text diagnostic results for narrow and redirected terminals."""

from __future__ import annotations

from rich.text import Text

from codex_mgr.core.doctor import run_diagnostics
from codex_mgr.utils.display import console, single_line

_STATUS = {
    "passed": ("OK", "success"),
    "failed": ("FAIL", "error"),
    "warning": ("WARN", "warning"),
    "not_checked": ("NOT CHECKED", "muted"),
}


def render_doctor_report(*, network: bool = False) -> bool:
    """Return false only for a failed check; never imply workflow readiness."""
    with console.status("Running diagnostics..."):
        results = run_diagnostics(network=network)
    console.print(Text("Codex diagnostics", style="bold accent"))
    for result in results:
        label, style = _STATUS[result.status]
        console.print()
        console.print(Text.assemble((label + "  ", style), (single_line(result.name), "bold")))
        console.print(Text(single_line(result.details)))
        if result.remediation:
            console.print(Text(single_line(result.remediation), style="warning"))
    failures = sum(result.status == "failed" for result in results)
    unchecked = sum(result.status == "not_checked" for result in results)
    warnings = sum(result.status == "warning" for result in results)
    console.print()
    console.print(Text(f"{failures} failed; {warnings} warnings; {unchecked} not checked."))
    return not failures
