"""Rich terminal report visualizer for environment diagnostics."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from codex_mgr.core.doctor import run_diagnostics
from codex_mgr.utils.display import console, get_symbol


def render_doctor_report() -> None:
    """Run diagnostics and render a high-polish summary table."""
    with console.status("[accent]Running system diagnostics...", spinner="dots"):
        results = run_diagnostics()

    table = Table(
        title=f"{get_symbol('gear')} Codex Environment Diagnostic Report",
        header_style="bold cyan",
        border_style="dim",
        expand=False
    )
    table.add_column("Component", style="bold", min_width=25)
    table.add_column("Status", justify="center", width=12)
    table.add_column("Details", style="dim", min_width=45)

    all_passed = True
    for r in results:
        if r.passed:
            status = f"[bold success]{get_symbol('check')} PASSED[/]"
        else:
            status = f"[bold error]{get_symbol('cross')} FAILED[/]"
            all_passed = False

        table.add_row(r.name, status, r.details)

    console.print()
    console.print(table)
    console.print()

    if all_passed:
        console.print(
            Panel(
                f"[bold success]{get_symbol('check')} ALL CHECKS HEALTHY[/]\n"
                "Your Codex environment and model providers are ready for seamless switching!",
                border_style="success",
                expand=False
            )
        )
    else:
        remediations = [f"• [bold]{r.name}:[/] {r.remediation}" for r in results if r.remediation]
        if remediations:
            console.print(
                Panel(
                    "[bold warning]Recommended Actions:[/]\n" + "\n".join(remediations),
                    border_style="warning",
                    expand=False
                )
            )
