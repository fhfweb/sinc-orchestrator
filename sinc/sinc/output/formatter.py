"""Terminal output formatter — rich-based, screenshot-worthy.

Degrades gracefully when rich is not installed.
"""
from __future__ import annotations

import os
from collections import Counter

def entropy_label(score: float) -> str:
    if score < 0.35: return "healthy"
    if score < 0.60: return "watch"
    if score < 0.80: return "refactor"
    if score < 0.85: return "critical"
    return "structural_hazard"

try:
    from rich.console import Console
    from rich.panel    import Panel
    from rich.table    import Table
    from rich.text     import Text
    from rich          import box as rich_box
    _RICH = True
except ImportError:
    _RICH = False

console: "Console | None" = Console(highlight=False) if _RICH else None

# ── Style maps ──────────────────────────────────────────────────────────────────
_LABEL_STYLE = {
    "structural_hazard": "bold magenta",
    "critical":          "bold red",
    "refactor":          "bold yellow",
    "watch":             "bold cyan",
    "healthy":           "bold green",
}
_LABEL_ICON = {
    "structural_hazard": "☠",
    "critical":          "●",
    "refactor":          "◕",
    "watch":             "◑",
    "healthy":           "○",
}
_LABEL_ORDER = ("healthy", "watch", "refactor", "critical", "structural_hazard")
_BAR_FULL  = "█"
_BAR_EMPTY = "░"
_BAR_WIDTH = 22


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _bar(frac: float, width: int = _BAR_WIDTH) -> str:
    filled = round(frac * width)
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def _short_path(path: str, max_len: int = 62) -> str:
    path = path.replace("\\", "/")
    if len(path) <= max_len:
        return path
    parts = path.split("/")
    if len(parts) > 3:
        return "…/" + "/".join(parts[-3:])
    return path


def _avg_score(metrics: list["FileMetrics"]) -> float:
    if not metrics:
        return 0.0
    return sum(m.score for m in metrics) / len(metrics)


# ── Public API ──────────────────────────────────────────────────────────────────

def print_scan_result(
    source:  str,
    metrics: list["FileMetrics"],
    branch:  str = "",
    verbose: bool = False,
) -> None:
    """Print the standard scan summary (header + health bar + top risks)."""
    if not metrics:
        if console:
            console.print("\n  [dim]No source files found.[/dim]\n")
        else:
            print("No source files found.")
        return

    if _RICH:
        _rich_summary(source, metrics, branch, verbose)
    else:
        _plain_summary(source, metrics)


def print_report(
    source:  str,
    metrics: list["FileMetrics"],
    branch:  str = "",
) -> None:
    """Print a detailed report: summary + module breakdown + hotspot analysis."""
    print_scan_result(source, metrics, branch, verbose=True)

    if not _RICH or not metrics:
        return

    # ── Worst modules (avg entropy) ──────────────────────────────────────────
    modules: dict[str, list["FileMetrics"]] = {}
    for m in metrics:
        mod = os.path.dirname(m.path).replace("\\", "/") or "."
        modules.setdefault(mod, []).append(m)

    worst = sorted(
        modules.items(),
        key=lambda kv: _avg_score(kv[1]),
        reverse=True,
    )[:12]

    if worst:
        console.print("  [bold]WORST MODULES  [dim](avg entropy)[/dim][/bold]\n")
        tbl = Table(box=None, show_header=False, padding=(0, 2))
        tbl.add_column("avg",   style="bold", width=6)
        tbl.add_column("files", width=6)
        tbl.add_column("module")
        for mod, mets in worst:
            avg   = _avg_score(mets)
            style = _LABEL_STYLE[entropy_label(avg)]
            tbl.add_row(
                f"[{style}]{avg:.2f}[/{style}]",
                f"[dim]{len(mets):>3}[/dim]",
                f"[dim]{mod}[/dim]",
            )
        console.print(tbl)
        console.print()

    # ── Circular dependencies ────────────────────────────────────────────────
    circular = [m for m in metrics if m.circular]
    if circular:
        console.print(f"  [bold yellow]CIRCULAR DEPENDENCIES[/bold yellow]  "
                      f"[dim]({len(circular)} files)[/dim]\n")
        for m in circular[:8]:
            console.print(f"    [yellow]{_short_path(m.path)}[/yellow]")
        if len(circular) > 8:
            console.print(f"    [dim]… and {len(circular) - 8} more[/dim]")
        console.print()

    # ── Risky files without tests ────────────────────────────────────────────
    no_tests = [m for m in metrics
                if not m.has_tests and m.label in ("critical", "refactor")]
    if no_tests:
        console.print(f"  [bold]RISKY FILES WITHOUT TESTS[/bold]  "
                      f"[dim]({len(no_tests)} files)[/dim]\n")
        for m in no_tests[:8]:
            style = _LABEL_STYLE[m.label]
            console.print(f"    [{style}]{m.score:.2f}[/{style}]  [dim]{_short_path(m.path)}[/dim]")
        if len(no_tests) > 8:
            console.print(f"    [dim]… and {len(no_tests) - 8} more[/dim]")
        console.print()


# ── Rich rendering ──────────────────────────────────────────────────────────────

def _rich_summary(
    source:  str,
    metrics: list["FileMetrics"],
    branch:  str,
    verbose: bool,
) -> None:
    counts  = Counter(m.label for m in metrics)
    total   = len(metrics)
    modules = len({os.path.dirname(m.path) for m in metrics})
    avg     = _avg_score(metrics)

    # ── Header panel ────────────────────────────────────────────────────────
    hdr = Text()
    hdr.append("SINC", style="bold white")
    hdr.append("  —  Software Architecture Intelligence\n", style="dim white")
    hdr.append(f"\n  Repository  :  ", style="dim")
    hdr.append(source, style="bold cyan")
    if branch:
        hdr.append(f"\n  Branch      :  ", style="dim")
        hdr.append(branch, style="cyan")
    hdr.append(f"\n\n  Files       :  ", style="dim")
    hdr.append(f"{total:,}", style="bold")
    hdr.append(f"   Modules  :  ", style="dim")
    hdr.append(f"{modules:,}", style="bold")
    hdr.append(f"   Avg entropy  :  ", style="dim")
    avg_style = _LABEL_STYLE[entropy_label(avg)]
    hdr.append(f"{avg:.2f}", style=avg_style)
    console.print()
    console.print(Panel(hdr, border_style="bright_black", padding=(0, 2)))
    console.print()

    # ── Architecture Health bars ─────────────────────────────────────────────
    console.print("  [bold]ARCHITECTURE HEALTH[/bold]\n")
    for lbl in _LABEL_ORDER:
        n     = counts.get(lbl, 0)
        if n == 0 and lbl == "structural_hazard":
            continue   # hide if none; don't clutter clean projects
        pct   = n / total if total else 0.0
        style = _LABEL_STYLE[lbl]
        icon  = _LABEL_ICON[lbl]
        bar   = _bar(pct)
        display = "Hazard" if lbl == "structural_hazard" else lbl.capitalize()
        console.print(
            f"  [{style}]{icon}  {display:<10}[/{style}]"
            f"  [dim]{bar}[/dim]"
            f"  [bold]{pct:5.0%}[/bold]"
            f"  [dim]({n:,} files)[/dim]"
        )
    console.print()

    # ── Top risk files ────────────────────────────────────────────────────────
    top = [m for m in metrics if m.label in ("structural_hazard", "critical", "refactor")]
    if not top:
        top = metrics[:5]
    top = top[:15]

    if top:
        has_churn   = any(m.churn_count > 0 for m in top)
        has_dominant = any(m.dominant_metric for m in top)
        console.print("  [bold]TOP RISK FILES[/bold]\n")
        tbl = Table(box=None, show_header=False, padding=(0, 1))
        tbl.add_column("score",  width=6)
        tbl.add_column("label",  width=18)
        if has_churn:
            tbl.add_column("churn", width=7)
        if has_dominant:
            tbl.add_column("driver", width=13)
        tbl.add_column("path")

        for m in top:
            style = _LABEL_STYLE[m.label]
            row = [
                f"[{style}]{m.score:.2f}[/{style}]",
                f"[{style}]{_LABEL_ICON[m.label]} {m.label}[/{style}]",
            ]
            if has_churn:
                churn_str = f"[dim]{m.churn_count}c[/dim]" if m.churn_count else "[dim]—[/dim]"
                row.append(churn_str)
            if has_dominant:
                dom = f"[dim]{m.dominant_metric}[/dim]" if m.dominant_metric else "[dim]—[/dim]"
                row.append(dom)
            row.append(f"[dim]{_short_path(m.path)}[/dim]")
            tbl.add_row(*row)
        console.print(tbl)
        console.print()

    # ── Verbose hotspots ──────────────────────────────────────────────────────
    if verbose:
        _rich_hotspots(metrics)

    # ── Footer / suggestion ───────────────────────────────────────────────────
    n_hazard = counts.get("structural_hazard", 0)
    n_crit   = counts.get("critical", 0)
    n_ref    = counts.get("refactor", 0)
    if n_hazard > 0:
        console.print(
            f"  [bold magenta]{_LABEL_ICON['structural_hazard']} {n_hazard}[/bold magenta]"
            f" [dim]structural hazard file(s) — immediate action required.[/dim]"
        )
    elif n_crit > 0:
        console.print(
            f"  [bold red]{n_crit}[/bold red] [dim]critical file(s) detected"
            f" — run [/dim][bold]sinc report[/bold][dim] for details.[/dim]"
        )
    elif n_ref > 0:
        console.print(
            f"  [dim]→ {n_ref} file(s) need refactoring"
            f" — run [/dim][bold]sinc report[/bold][dim] for details.[/dim]"
        )
    else:
        console.print("  [bold green]✓  Architecture looks healthy.[/bold green]")
    console.print()


def _rich_hotspots(metrics: list["FileMetrics"]) -> None:
    """Print structural hazards, circular deps, untested-risky, and co-change sections."""
    # ── Structural hazards ───────────────────────────────────────────────────
    hazards = [m for m in metrics if m.label == "structural_hazard"]
    if hazards:
        console.print(
            f"  [bold magenta]{_LABEL_ICON['structural_hazard']} STRUCTURAL HAZARDS[/bold magenta]"
            f"  [dim]({len(hazards)} files — dominance penalty applied)[/dim]\n"
        )
        for m in hazards[:8]:
            driver = f"  [dim]driver: {m.dominant_metric}[/dim]" if m.dominant_metric else ""
            console.print(
                f"    [bold magenta]{m.score:.2f}[/bold magenta]"
                f"  [dim]{_short_path(m.path)}[/dim]{driver}"
            )
        if len(hazards) > 8:
            console.print(f"    [dim]… and {len(hazards) - 8} more[/dim]")
        console.print()

    # ── Circular dependencies ────────────────────────────────────────────────
    circular = [m for m in metrics if m.circular]
    if circular:
        console.print(
            f"  [bold yellow]CIRCULAR DEPENDENCIES[/bold yellow]"
            f"  [dim]({len(circular)} files)[/dim]\n"
        )
        for m in circular[:6]:
            console.print(f"    [yellow]{_short_path(m.path)}[/yellow]")
        if len(circular) > 6:
            console.print(f"    [dim]… and {len(circular) - 6} more[/dim]")
        console.print()

    # ── Risky files without tests ────────────────────────────────────────────
    no_tests = [m for m in metrics
                if not m.has_tests and m.label in ("structural_hazard", "critical", "refactor")]
    if no_tests:
        console.print(
            f"  [bold]RISKY FILES WITHOUT TESTS[/bold]"
            f"  [dim]({len(no_tests)} files)[/dim]\n"
        )
        for m in no_tests[:6]:
            style = _LABEL_STYLE[m.label]
            console.print(
                f"    [{style}]{m.score:.2f}[/{style}]"
                f"  [dim]{_short_path(m.path)}[/dim]"
            )
        if len(no_tests) > 6:
            console.print(f"    [dim]… and {len(no_tests) - 6} more[/dim]")
        console.print()

    # ── High-churn files (git data available) ────────────────────────────────
    churny = sorted(
        [m for m in metrics if m.churn_count > 0],
        key=lambda m: m.churn_count, reverse=True,
    )[:8]
    if churny:
        console.print(
            f"  [bold]HIGH-CHURN FILES[/bold]"
            f"  [dim](changed most frequently in git history)[/dim]\n"
        )
        tbl = Table(box=None, show_header=False, padding=(0, 1))
        tbl.add_column("churn",   width=6)
        tbl.add_column("entropy", width=7)
        tbl.add_column("path")
        for m in churny:
            style = _LABEL_STYLE[m.label]
            tbl.add_row(
                f"[bold]{m.churn_count}c[/bold]",
                f"[{style}]{m.score:.2f}[/{style}]",
                f"[dim]{_short_path(m.path)}[/dim]",
            )
        console.print(tbl)
        console.print()

    # ── Co-change pairs (implicit coupling) ──────────────────────────────────
    co_changed = [m for m in metrics if m.co_change_score > 0.3]
    if co_changed:
        console.print(
            f"  [bold]IMPLICIT COUPLING  [dim](files that always change together)[/dim][/bold]\n"
        )
        for m in sorted(co_changed, key=lambda x: x.co_change_score, reverse=True)[:6]:
            console.print(
                f"    [yellow]{m.co_change_score:.0%}[/yellow]  [dim]{_short_path(m.path)}[/dim]"
            )
        console.print()


# ── Plain-text fallback ─────────────────────────────────────────────────────────

def _plain_summary(source: str, metrics: list["FileMetrics"]) -> None:
    total  = len(metrics)
    counts = Counter(m.label for m in metrics)
    avg    = _avg_score(metrics)

    print(f"\nSINC — Software Architecture Intelligence")
    print(f"{'─' * 50}")
    print(f"Repository    : {source}")
    print(f"Files         : {total:,}")
    print(f"Avg entropy   : {avg:.2f}  ({entropy_label(avg)})")
    print()
    print("ARCHITECTURE HEALTH")
    for lbl in ("healthy", "watch", "refactor", "critical"):
        n   = counts.get(lbl, 0)
        pct = n / total * 100 if total else 0.0
        bar = _bar(pct / 100)
        print(f"  {lbl.capitalize():<10}  {bar}  {pct:5.1f}%  ({n:,} files)")
    print()
    print("TOP RISK FILES")
    for m in metrics[:10]:
        print(f"  {m.score:.2f}  {m.path}")
    print()
