# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rw`: the Reelwright CLI."""

from __future__ import annotations

import signal
import socket
import threading
from types import FrameType

import typer
import uvicorn
from sqlmodel import Session

from reelwright.api.projects import list_projects
from reelwright.db.session import get_engine, migrate
from reelwright.render.picker import caps_of
from reelwright.render.registry import live_workers, utcnow

app = typer.Typer(help="Reelwright: AI-run creator studio.", no_args_is_help=True)
projects_app = typer.Typer(help="Video projects.", no_args_is_help=True)
app.add_typer(projects_app, name="projects")
worker_app = typer.Typer(help="Run a render worker on this machine.", no_args_is_help=True)
app.add_typer(worker_app, name="worker")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (tailnet exposure is via deploy)."),
    port: int = typer.Option(8000, help="Port."),
    reload: bool = typer.Option(False, help="Reload on code changes (development)."),
) -> None:
    """Run the API and dashboard."""
    uvicorn.run("reelwright.api.app:create_app", factory=True, host=host, port=port, reload=reload)


@app.command("migrate")
def migrate_command() -> None:
    """Apply database migrations up to head (deploys run this before restarting)."""
    migrate()
    typer.echo("Migrations applied.")


@app.command()
def scheduler(
    interval: float = typer.Option(60.0, help="Seconds between ticks."),
    once: bool = typer.Option(False, help="Run a single tick and exit."),
) -> None:
    """Run the job scheduler (idle until the job and publish queues land)."""
    migrate()
    stop = threading.Event()
    if not once:

        def _stop(_signum: int, _frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    while True:
        typer.echo("scheduler: tick (no jobs queued)")
        if once or stop.wait(interval):
            break
    typer.echo("scheduler: stopped")


@projects_app.command("list")
def projects_list() -> None:
    """List projects, newest first."""
    migrate()
    with Session(get_engine()) as session:
        rows = list_projects(session)
    if not rows:
        typer.echo("No projects.")
        return
    for p in rows:
        typer.echo(f"{p.slug}\t{p.title}\t{p.format.value}\t{p.status}")


@app.command("workers")
def workers_list() -> None:
    """List live render workers with GPU stats."""
    migrate()
    with Session(get_engine()) as session:
        rows = live_workers(session, utcnow())
    if not rows:
        typer.echo("No live workers.")
        return
    for w in rows:
        state = "yield" if w.yielding else "busy" if w.busy else "ready"
        caps = ",".join(sorted(caps_of(w.capabilities))) or "-"
        typer.echo(
            f"{w.name}\t{w.host or '-'}\t{w.gpu_model or 'cpu'}\t"
            f"{w.vram_free_mb}/{w.vram_total_mb} MB\t{w.utilization_pct}%\t"
            f"{w.temperature_c}C\t{caps}\t{state}"
        )


@worker_app.command("run")
def worker_run(
    server: str = typer.Option(
        ..., envvar="REELWRIGHT_SERVER", help="API base URL: https:// on the tailnet."
    ),
    name: str = typer.Option(socket.gethostname(), help="Worker name (one per card)."),
    cpu: bool = typer.Option(False, help="CPU fallback worker: same job kinds, no GPU."),
    interval: float = typer.Option(5.0, help="Seconds between polls when idle."),
    mem_cap_mb: int | None = typer.Option(None, help="RAM cap per job process, in MB."),
) -> None:
    """Heartbeat and pull jobs. The token comes from REELWRIGHT_WORKER_TOKEN."""
    from reelwright.render.worker import Agent, ResourcePolicy

    agent = Agent(server, name, cpu=cpu, policy=ResourcePolicy(nice=True, mem_cap_mb=mem_cap_mb))
    agent.run_forever(interval)


if __name__ == "__main__":
    app()
