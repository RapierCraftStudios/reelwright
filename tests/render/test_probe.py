# SPDX-License-Identifier: AGPL-3.0-or-later
from reelwright.render.probe import (
    COMPUTE_APPS_QUERY,
    NVIDIA_SMI_QUERY,
    Runner,
    compute_yield,
    parse_compute_apps,
    parse_nvidia_smi,
    probe,
)


def no_ffmpeg(_: str) -> None:
    return None


SMI = "NVIDIA GeForce RTX 4070 Ti, 12282, 10850, 7, 41\n"
LAPTOP = "NVIDIA GeForce RTX 3070 Laptop GPU, 8192, [N/A], 0, 38\n"


def fake_run(gpus: str | None, apps: str = "") -> Runner:
    def run(argv: list[str]) -> str | None:
        if argv == NVIDIA_SMI_QUERY:
            return gpus
        if argv == COMPUTE_APPS_QUERY:
            return apps
        raise AssertionError(argv)

    return run


def test_parse_nvidia_smi() -> None:
    (card,) = parse_nvidia_smi(SMI)
    assert (card.model, card.vram_total_mb, card.vram_free_mb) == (
        "NVIDIA GeForce RTX 4070 Ti",
        12282,
        10850,
    )
    assert (card.utilization_pct, card.temperature_c) == (7, 41)
    (laptop,) = parse_nvidia_smi(LAPTOP)
    assert (laptop.vram_total_mb, laptop.vram_free_mb) == (8192, 0)
    assert parse_nvidia_smi("") == []


def test_parse_compute_apps() -> None:
    assert parse_compute_apps("1234, 3000\n55, [N/A]\n") == [(1234, 3000), (55, 0)]


def test_compute_yield() -> None:
    assert compute_yield(True, []) is True
    assert compute_yield(False, [2048]) is True
    assert compute_yield(False, [200, 300]) is False


def test_probe_gpu_snapshot() -> None:
    snap = probe(run=fake_run(SMI), which=lambda _: "/usr/bin/ffmpeg", fullscreen=lambda: False)
    assert snap["gpu_model"] == "NVIDIA GeForce RTX 4070 Ti"
    assert snap["vram_free_mb"] == 10850 and snap["temperature_c"] == 41
    assert set(snap["capabilities"]) == {"cuda", "nvenc", "ffmpeg"}
    assert snap["yielding"] is False


def test_probe_yields_for_fullscreen_or_heavy_foreign_process() -> None:
    assert probe(run=fake_run(SMI), fullscreen=lambda: True, which=no_ffmpeg)["yielding"] is True
    heavy = fake_run(SMI, "4242, 6000\n")
    assert probe(run=heavy, fullscreen=lambda: False, which=no_ffmpeg)["yielding"] is True
    own = probe(run=heavy, fullscreen=lambda: False, own_pids={4242}, which=no_ffmpeg)
    assert own["yielding"] is False


def test_probe_without_nvidia_smi_is_cpu_snapshot() -> None:
    snap = probe(run=fake_run(None), which=no_ffmpeg, fullscreen=lambda: False)
    assert snap["capabilities"] == ["cpu"] and snap["vram_total_mb"] == 0
    cpu_only = probe(gpu=False, run=fake_run(SMI), which=no_ffmpeg, fullscreen=lambda: False)
    assert cpu_only["gpu_model"] == ""
