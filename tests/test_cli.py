# SPDX-License-Identifier: AGPL-3.0-or-later
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from reelwright.cli.main import app

runner = CliRunner()


def test_projects_list_empty(tmp_db: str) -> None:
    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert "No projects." in result.output


def test_projects_list_shows_project_created_through_api(client: TestClient) -> None:
    client.post("/api/projects", json={"title": "CLI Sees Me", "format": "short"})
    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert "cli-sees-me\tCLI Sees Me\tshort\tdraft" in result.output


def test_migrate_creates_the_schema(tmp_db: str) -> None:
    from sqlalchemy import create_engine, inspect

    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert "project" in inspect(create_engine(tmp_db)).get_table_names()


def test_scheduler_once_ticks_and_exits(tmp_db: str) -> None:
    result = runner.invoke(app, ["scheduler", "--once"])
    assert result.exit_code == 0, result.output
    assert "scheduler: tick" in result.output
