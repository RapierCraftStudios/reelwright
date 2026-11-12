# SPDX-License-Identifier: AGPL-3.0-or-later
"""`rw`: the Reelwright CLI."""

from __future__ import annotations

import typer
import uvicorn
from sqlmodel import Session

from reelwright.api.projects import list_projects
from reelwright.db.session import get_engine, migrate

app = typer.Typer(help="Reelwright: AI-run creator studio.", no_args_is_help=True)
projects_app = typer.Typer(help="Video projects.", no_args_is_help=True)
app.add_typer(projects_app, name="projects")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address (tailnet exposure is via deploy)."),
    port: int = typer.Option(8000, help="Port."),
    reload: bool = typer.Option(False, help="Reload on code changes (development)."),
) -> None:
    """Run the API and dashboard."""
    uvicorn.run("reelwright.api.app:create_app", factory=True, host=host, port=port, reload=reload)


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


if __name__ == "__main__":
    app()
