"""
Typer CLI for Computer-Use Automation System.
Provides developer workflow commands for:
- serve-target: Local mock legacy banking application server
- discover: LLM discovery agent against live UI
- replay: Deterministic, zero-LLM capability replay
- test-harness: End-to-end test and validation suite
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.agent.compiler import ArtifactCompiler
from src.agent.discovery import DiscoveryAgent
from src.engine.executor import ReplayExecutor
from src.guardrails.policy import GuardrailPolicy
from src.guardrails.redactor import PIIRedactor
from src.human.escalation import EscalationManager
from src.models.artifact import CapabilityArtifact
from src.models.result import ExecutionResult, ExecutionStatus

app = typer.Typer(
    name="cua",
    help="Computer-Use Automation System for Legacy Banking Applications",
    add_completion=False,
)
console = Console()


def _format_result(result: ExecutionResult) -> None:
    """Renders a rich table and panel for an ExecutionResult."""
    status_style = {
        ExecutionStatus.SUCCESS: "bold green",
        ExecutionStatus.BUSINESS_OUTCOME: "bold yellow",
        ExecutionStatus.RECOVERABLE_ERROR: "bold magenta",
        ExecutionStatus.HARD_FAILURE: "bold red",
    }.get(result.status, "white")

    console.print(
        Panel(
            f"[bold]Capability:[/bold] {result.capability_id} (v{result.version})\n"
            f"[bold]Status:[/bold] [{status_style}]{result.status.value}[/{status_style}]\n"
            f"[bold]Outcome Code:[/bold] {result.outcome_code or 'N/A'}\n"
            f"[bold]Execution Time:[/bold] {result.execution_time_ms} ms\n"
            f"[bold]Steps Executed:[/bold] {result.steps_executed}",
            title=f"[{status_style}]Execution Result: {result.status.value}[/{status_style}]",
            border_style=status_style.split()[-1],
        )
    )

    if result.data:
        data_table = Table(title="Extracted Output Data", show_header=True)
        data_table.add_column("Key", style="cyan", no_wrap=True)
        data_table.add_column("Value", style="green")
        for k, v in result.data.items():
            data_table.add_row(str(k), str(v))
        console.print(data_table)

    if result.step_logs:
        steps_table = Table(title="Step Execution Trace", show_header=True)
        steps_table.add_column("Step", style="dim", width=6)
        steps_table.add_column("Action", style="cyan", width=12)
        steps_table.add_column("Locator Used", style="blue")
        steps_table.add_column("Status", width=10)
        steps_table.add_column("Duration (ms)", justify="right", width=14)

        for log in result.step_logs:
            s_style = "green" if log.status == "OK" else "red"
            steps_table.add_row(
                str(log.step_id),
                log.action,
                log.target_resolved or "-",
                f"[{s_style}]{log.status}[/{s_style}]",
                str(log.duration_ms),
            )
        console.print(steps_table)

    if result.interventions:
        console.print(
            f"[yellow]Human interventions recorded: {len(result.interventions)}[/yellow]"
        )


@app.command("serve-target")
def serve_target(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Bind host address"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload on code change"),
) -> None:
    """Launch the local mock core banking portal."""
    console.print(f"[bold green]Starting Core Banking Target Portal on http://{host}:{port}[/bold green]")
    uvicorn.run("src.target_app.app:app", host=host, port=port, reload=reload)


@app.command("replay")
def replay(
    artifact_path: Path = typer.Option(
        ..., "--artifact", "-a", help="Path to CapabilityArtifact JSON file"
    ),
    params: Optional[str] = typer.Option(
        None, "--params", "-p", help="JSON dictionary of input parameters, e.g. '{\"member_id\": \"12345\"}'"
    ),
    headless: bool = typer.Option(True, "--headless/--headed", help="Run browser in headless or headed mode"),
    allow_irreversible: bool = typer.Option(
        False, "--allow-irreversible", help="Authorize execution of steps marked is_irreversible: true"
    ),
    evidence_dir: Path = typer.Option(
        Path("evidence"), "--evidence-dir", help="Directory where diagnostic evidence is stored"
    ),
) -> None:
    """Replay a capability artifact deterministically without LLM."""
    if not artifact_path.exists():
        console.print(f"[bold red]Error: Artifact file not found:[/bold red] {artifact_path}")
        raise typer.Exit(code=1)

    try:
        raw_json = artifact_path.read_text(encoding="utf-8")
        artifact = CapabilityArtifact.model_validate_json(raw_json)
    except Exception as exc:
        console.print(f"[bold red]Failed to parse artifact JSON:[/bold red] {exc}")
        raise typer.Exit(code=1)

    parsed_params: Dict[str, Any] = {}
    if params:
        try:
            parsed_params = json.loads(params)
        except Exception as exc:
            console.print(f"[bold red]Invalid --params JSON string:[/bold red] {exc}")
            raise typer.Exit(code=1)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    redactor = PIIRedactor()
    policy = GuardrailPolicy()
    escalation = EscalationManager(evidence_dir=str(evidence_dir), redactor=redactor)

    executor = ReplayExecutor(
        policy=policy,
        redactor=redactor,
        escalation_manager=escalation,
        headless=headless,
        evidence_dir=str(evidence_dir),
        allow_irreversible=allow_irreversible,
    )

    console.print(
        f"[cyan]Replaying capability [bold]{artifact.name}[/bold] ({artifact.id} v{artifact.version})...[/cyan]"
    )
    result = asyncio.run(executor.execute(artifact, inputs=parsed_params))
    _format_result(result)

    if result.status == ExecutionStatus.HARD_FAILURE:
        raise typer.Exit(code=1)


@app.command("discover")
def discover(
    goal: str = typer.Option(..., "--goal", "-g", help="High-level goal for the discovery agent"),
    url: str = typer.Option(..., "--url", "-u", help="Starting URL for discovery"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Target path to write the compiled CapabilityArtifact JSON"
    ),
    capability_id: Optional[str] = typer.Option(
        None, "--id", help="Capability identifier (e.g. member_savings_lookup)"
    ),
    capability_name: Optional[str] = typer.Option(
        None, "--name", help="Human-readable capability name"
    ),
    target_path: Optional[str] = typer.Option(
        None, "--target-path", help="Target URL path (e.g. /members)"
    ),
    model: str = typer.Option("gpt-4o", "--model", help="OpenAI chat model"),
    max_steps: int = typer.Option(15, "--max-steps", help="Maximum steps before stopping"),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", envvar="OPENAI_API_KEY", help="OpenAI API key"
    ),
    headless: bool = typer.Option(True, "--headless/--headed", help="Run browser in headless or headed mode"),
) -> None:
    """Run LLM discovery agent against live UI to explore and compile a capability artifact."""
    cid = capability_id or re.sub(r"[^a-zA-Z0-9_]+", "_", goal.lower()).strip("_")[:30]
    cname = capability_name or goal[:50]

    openai_client = None
    if api_key:
        try:
            from openai import AsyncOpenAI
            openai_client = AsyncOpenAI(api_key=api_key)
        except Exception:
            pass

    agent = DiscoveryAgent(
        headless=headless,
        max_steps=max_steps,
        openai_client=openai_client,
    )

    console.print(f"[cyan]Starting autonomous discovery for goal: [bold]{goal}[/bold]...[/cyan]")
    try:
        disc_result = asyncio.run(
            agent.discover(
                goal=goal,
                target_url=url,
                capability_id=cid,
                capability_name=cname,
            )
        )
    except Exception as exc:
        console.print(f"[bold red]Discovery failed with exception:[/bold red] {exc}")
        raise typer.Exit(code=1)

    if not disc_result.success or not disc_result.capability:
        console.print(f"[bold red]Discovery failed:[/bold red] {disc_result.error_message or 'Unknown error'}")
        raise typer.Exit(code=1)

    artifact = disc_result.capability

    console.print(
        Panel(
            f"[bold green]Discovery Succeeded![/bold green]\n"
            f"[bold]Capability ID:[/bold] {artifact.id}\n"
            f"[bold]Name:[/bold] {artifact.name}\n"
            f"[bold]Steps Synthesized:[/bold] {len(artifact.steps)}\n"
            f"[bold]Declared Inputs:[/bold] {list(artifact.input_schema.keys())}\n"
            f"[bold]Declared Outputs:[/bold] {list(artifact.output_schema.keys())}",
            title="[bold green]Compiled Capability Artifact[/bold green]",
            border_style="green",
        )
    )

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[bold green]Saved artifact to:[/bold green] {output}")
    else:
        console.print(artifact.model_dump_json(indent=2))


@app.command("test-harness")
def test_harness(
    coverage: bool = typer.Option(True, "--coverage/--no-coverage", help="Run with coverage report"),
) -> None:
    """Run the complete end-to-end verification and test suite."""
    import subprocess
    import sys

    console.print("[bold cyan]Running automated test harness...[/bold cyan]")
    cmd = [sys.executable, "-m", "pytest"]
    if coverage:
        cmd.extend(["--cov=src", "--cov-report=term-missing"])

    res = subprocess.run(cmd)
    if res.returncode != 0:
        console.print("[bold red]Test harness detected failures![/bold red]")
        raise typer.Exit(code=res.returncode)
    console.print("[bold green]Test harness passed all checks successfully![/bold green]")


if __name__ == "__main__":
    app()
