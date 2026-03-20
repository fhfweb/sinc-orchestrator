"""sinc CLI — Software Architecture Intelligence

Thin client for the SINC Orchestrator.  All scanning logic lives in the
Orchestrator's entropy_scanner service.  The CLI sends HTTP requests and
formats the results for the terminal.

Usage:
  sinc scan [PATH]                          # scan local repo via Orchestrator
  sinc scan . --report                      # full report
  sinc connect https://github.com/org/repo  # clone + scan
  sinc connect <url> --branch dev --report  # full report on specific branch
  sinc report [PATH]                        # detailed report (local)
  sinc report . --label critical            # filter by label

Configuration:
  SINC_ORCHESTRATOR_URL   Orchestrator base URL  (default: http://localhost:8765)
  SINC_API_KEY            API key if auth is enabled on the Orchestrator
"""
from __future__ import annotations

import os
import shutil
import sys
import json
from typing import Optional

import click

try:
    from rich.console  import Console
    from rich.progress import (BarColumn, Progress, SpinnerColumn,
                               TaskProgressColumn, TextColumn)
    _RICH   = True
    console = Console(highlight=False)
except ImportError:
    _RICH   = False
    console = None  # type: ignore[assignment]

from sinc.output.formatter import print_scan_result, print_report

# ── Orchestrator client ──────────────────────────────────────────────────────

_ORCH_URL = os.environ.get("SINC_ORCHESTRATOR_URL", "http://localhost:8765").rstrip("/")
_API_KEY  = os.environ.get("SINC_API_KEY", "")


def _scan_via_orchestrator(
    path:      str,
    label:     Optional[str] = None,
    min_score: float = 0.0,
    churn_map: Optional[dict] = None,
) -> list:
    """
    Call POST /entropy/scan-local on the Orchestrator.
    Returns a list of FileMetrics-compatible objects.
    Raises RuntimeError on connection failure.
    """
    try:
        import urllib.request

        payload = json.dumps({
            "path":      path,
            "label":     label or "",
            "min_score": min_score,
            "churn":     churn_map,
        }).encode()

        headers = {"Content-Type": "application/json"}
        if _API_KEY:
            headers["X-API-Key"] = _API_KEY

        req = urllib.request.Request(
            f"{_ORCH_URL}/entropy/scan-local",
            data=payload, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return _dicts_to_metrics(data.get("files", []))

    except OSError as exc:
        raise RuntimeError(
            f"Cannot reach Orchestrator at {_ORCH_URL}\n"
            f"  Make sure the Orchestrator is running:  docker compose up\n"
            f"  Or set SINC_ORCHESTRATOR_URL to the correct address.\n"
            f"  Detail: {exc}"
        ) from exc


def _dicts_to_metrics(files: list[dict]) -> list:
    """Convert API response dicts to simple namespace objects the formatter can use."""
    from types import SimpleNamespace
    result = []
    for f in files:
        if f.get("is_test"):
            continue
        result.append(SimpleNamespace(
            path            = f.get("file_path", ""),
            ext             = "." + f.get("language", ""),
            score           = f.get("entropy_score", 0.0),
            label           = f.get("label", "healthy"),
            dominant_metric = f.get("dominant_metric", ""),
            complexity      = f.get("complexity", 0),
            max_fn_lines    = f.get("max_fn_lines", 0),
            file_lines      = f.get("file_lines", 0),
            efferent        = f.get("efferent", 0),
            afferent        = f.get("afferent", 0),
            has_tests       = f.get("has_tests", False),
            circular        = f.get("circular_deps", False),
            duplication     = f.get("duplication", 0.0),
            instability     = f.get("instability", 1.0),
            blast_weight    = f.get("blast_weight", 0.0),
            martin_zone     = f.get("martin_zone", "neutral"),
            dep_entropy     = f.get("dep_entropy", 0.0),
            hotspot_score   = f.get("hotspot_score", 0.0),
            churn_count     = f.get("churn_count", 0),
            co_change_score = f.get("co_change_score", 0.0),
        ))
    return result


# ── CLI group ────────────────────────────────────────────────────────────────

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="sinc", prog_name="sinc")
def main() -> None:
    """SINC — Software Architecture Intelligence

    Terminal client for the SINC Orchestrator.
    All analysis runs inside the Orchestrator; this CLI formats and displays results.

    \b
    Quick start:
      sinc scan .
      sinc connect https://github.com/django/django

    \b
    Orchestrator URL (default: http://localhost:8765):
      export SINC_ORCHESTRATOR_URL=http://my-server:8765
    """


# ── sinc scan ────────────────────────────────────────────────────────────────

@main.command()
@click.argument("path", default=".", required=False)
@click.option("--report", "-r", is_flag=True,
              help="Show full report (module breakdown + hotspots).")
@click.option("--label", "-l",
              type=click.Choice(["structural_hazard", "critical", "refactor", "watch", "healthy"]),
              default=None, help="Filter output by entropy label.")
@click.option("--min-score", type=float, default=0.0, show_default=True,
              help="Only show files above this entropy score.")
def scan(path: str, report: bool, label: Optional[str], min_score: float) -> None:
    """Scan a local repository for architecture entropy.

    \b
    Examples:
      sinc scan .
      sinc scan /path/to/project --report
      sinc scan . --label critical
    """
    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        _err(f"Path not found: {abs_path}")

    metrics = _run_scan(abs_path, label=label, min_score=min_score)
    if report:
        print_report(abs_path, metrics)
    else:
        print_scan_result(abs_path, metrics)


# ── sinc connect ─────────────────────────────────────────────────────────────

@main.command()
@click.argument("repo_url")
@click.option("--branch", "-b", default="",
              help="Branch to clone (default: repo default branch).")
@click.option("--token", "-t", default="", envvar="GITHUB_TOKEN",
              show_envvar=True,
              help="GitHub access token for private repos.")
@click.option("--report", "-r", is_flag=True,
              help="Show full report (module breakdown + hotspots).")
@click.option("--label", "-l",
              type=click.Choice(["structural_hazard", "critical", "refactor", "watch", "healthy"]),
              default=None, help="Filter output by entropy label.")
def connect(repo_url: str, branch: str, token: str, report: bool,
            label: Optional[str]) -> None:
    """Clone and analyze a remote GitHub repository.

    \b
    Examples:
      sinc connect https://github.com/django/django
      sinc connect https://github.com/org/repo --branch main --report
      sinc connect https://github.com/org/private --token ghp_xxx
    """
    from sinc.repo.clone import clone as git_clone, detect_branch, repo_name_from_url

    display_name = repo_name_from_url(repo_url)

    if _RICH:
        console.print(f"\n  [dim]Cloning[/dim]  [bold cyan]{display_name}[/bold cyan]"
                      f"[dim]  …[/dim]")
    else:
        click.echo(f"Cloning {display_name} …")

    clone_path: Optional[str] = None
    try:
        clone_path    = git_clone(repo_url, branch=branch, token=token)
        actual_branch = branch or detect_branch(clone_path)
        metrics       = _run_scan(clone_path, label=label)

        if report:
            print_report(display_name, metrics, branch=actual_branch)
        else:
            print_scan_result(display_name, metrics, branch=actual_branch)

    except RuntimeError as exc:
        _err(str(exc))
    finally:
        if clone_path and os.path.exists(clone_path):
            shutil.rmtree(clone_path, ignore_errors=True)


# ── sinc report ──────────────────────────────────────────────────────────────

@main.command()
@click.argument("path", default=".", required=False)
@click.option("--label", "-l",
              type=click.Choice(["structural_hazard", "critical", "refactor", "watch", "healthy"]),
              default=None, help="Filter output by entropy label.")
@click.option("--min-score", type=float, default=0.0, show_default=True,
              help="Only show files above this entropy score.")
def report(path: str, label: Optional[str], min_score: float) -> None:
    """Show a detailed architecture report for a local repository.

    \b
    Examples:
      sinc report .
      sinc report /path/to/project --label refactor
      sinc report . --min-score 0.6
    """
    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        _err(f"Path not found: {abs_path}")

    metrics = _run_scan(abs_path, label=label, min_score=min_score)
    print_report(abs_path, metrics)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _run_scan(
    project_path: str,
    label:        Optional[str] = None,
    min_score:    float = 0.0,
) -> list:
    """Call the Orchestrator and return metrics list."""
    if _RICH:
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("  [dim]Scanning via Orchestrator[/dim]"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("", total=None)
            try:
                return _scan_via_orchestrator(project_path, label=label, min_score=min_score)
            except RuntimeError as exc:
                _err(str(exc))
    else:
        click.echo("Scanning via Orchestrator …")
        try:
            return _scan_via_orchestrator(project_path, label=label, min_score=min_score)
        except RuntimeError as exc:
            _err(str(exc))
    return []


def _err(msg: str) -> None:
    if _RICH:
        console.print(f"\n  [bold red]Error:[/bold red] [dim]{msg}[/dim]\n")
    else:
        click.echo(f"Error: {msg}", err=True)
    sys.exit(1)
