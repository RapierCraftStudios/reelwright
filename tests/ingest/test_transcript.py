# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from reelwright.db.models import RenderJob, Transcript
from reelwright.db.session import get_engine
from reelwright.ingest import transcript
from reelwright.ingest.transcript import words_from_segments
from reelwright.render import queue
from reelwright.render.queue import JobKind
from reelwright.render.worker import (
    CPU_EXECUTORS,
    GPU_EXECUTORS,
    TRANSCRIBE_JOB,
    Agent,
    ResourcePolicy,
)

SCREEN = {
    "path": "D:\\Recordly\\take-01.mp4",
    "host": "mrdubey",
    "checksum": "e" * 64,
    "size_bytes": 1000,
    "kind": "screen",
    "duration_s": 12.0,
}
GPU: dict[str, Any] = {
    "name": "mrdubey-4070ti",
    "host": "mrdubey",
    "gpu_model": "NVIDIA GeForce RTX 4070 Ti",
    "vram_total_mb": 12282,
    "vram_free_mb": 10850,
    "capabilities": ["nvenc", "cuda", "ffmpeg"],
}
CPU = {"name": "ci-cpu", "host": "ci", "capabilities": ["cpu", "ffmpeg"]}
WORDS = [
    {"word": "Hello", "start": 0.12, "end": 0.48, "confidence": 0.97},
    {"word": "world", "start": 0.52, "end": 0.9, "confidence": 0.88},
]
UPLOAD = {"engine": "whisperx", "model": "large-v3", "device": "cuda", "language": "en"}


def load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rw_transcribe_job", TRANSCRIBE_JOB)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cpu_probe(**_: Any) -> dict[str, Any]:
    return {"host": "ci", "capabilities": ["cpu", "ffmpeg"], "yielding": False}


def jobs_of(kind: JobKind) -> list[RenderJob]:
    with Session(get_engine()) as session:
        return list(session.exec(select(RenderJob).where(RenderJob.kind == kind.value)).all())


# --- R1: words with start/end and confidence ----------------------------------------------------


def test_words_from_whisperx_and_faster_whisper_shapes() -> None:
    whisperx_segments = [
        {
            "words": [
                {"word": " Hello", "start": 0.1, "end": 0.4, "score": 0.91},
                {"word": "2026", "score": 0.5},  # whisperx leaves numerals unaligned
                {"word": " ", "start": 0.4, "end": 0.5, "score": 0.9},
            ]
        },
        {"text": "no words"},
    ]
    fw_segments = [
        SimpleNamespace(words=[SimpleNamespace(word=" there", start=1.0, end=1.3, probability=0.8)])
    ]
    words = words_from_segments(whisperx_segments) + words_from_segments(fw_segments)
    assert [(w.word, w.start, w.end, w.confidence) for w in words] == [
        ("Hello", 0.1, 0.4, 0.91),
        ("there", 1.0, 1.3, 0.8),
    ]


def test_upload_rejects_bad_timings_and_confidence(client: TestClient) -> None:
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    url = f"/api/assets/{asset['id']}/transcript"
    backwards = {**UPLOAD, "words": [{"word": "a", "start": 2.0, "end": 1.0, "confidence": 0.5}]}
    too_sure = {**UPLOAD, "words": [{"word": "a", "start": 0.0, "end": 1.0, "confidence": 1.5}]}
    no_conf = {**UPLOAD, "words": [{"word": "a", "start": 0.0, "end": 1.0}]}
    for bad in (backwards, too_sure, no_conf):
        assert client.put(url, json=bad).status_code == 422
    assert client.put(url, content=b"not json").status_code == 422
    assert client.get(f"/api/assets/{asset['id']}").json()["transcript"] is None


def test_stored_transcript_round_trips_words(client: TestClient) -> None:
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    url = f"/api/assets/{asset['id']}/transcript"
    r = client.put(url, json={**UPLOAD, "words": WORDS})
    assert r.status_code == 200, r.text
    first = r.json()["transcript"]
    assert first["words"] == WORDS and first["text"] == "Hello world"
    again = client.put(url, json={**UPLOAD, "words": WORDS[:1]}).json()["transcript"]
    assert again["words"] == WORDS[:1]
    with Session(get_engine()) as session:
        rows = session.exec(select(Transcript)).all()
        assert [(r.id, r.word_count) for r in rows] == [(again["id"], 1)]  # replaced, not appended


# --- R2/R4: WhisperX then faster-whisper, GPU then CPU ------------------------------------------


def seg(word: str) -> list[dict[str, Any]]:
    return [{"words": [{"word": word, "start": 0.0, "end": 0.5, "score": 0.9}]}]


def test_job_prefers_whisperx_on_cuda() -> None:
    job = load_job()
    calls: list[tuple[str, str, str]] = []

    def wx(src: str, device: str, ctype: str, model: str) -> tuple[list[Any], str]:
        calls.append(("whisperx", device, ctype))
        return seg("hi"), "en"

    def fw(src: str, device: str, ctype: str, model: str) -> tuple[list[Any], str]:
        raise AssertionError("fallback must not run when whisperx works")

    out = job.transcribe("in.mov", "cuda", "float16", engines=[("whisperx", wx), ("fw", fw)])
    assert calls == [("whisperx", "cuda", "float16")]
    assert (out["engine"], out["device"], out["language"]) == ("whisperx", "cuda", "en")
    assert out["words"] == [{"word": "hi", "start": 0.0, "end": 0.5, "confidence": 0.9}]


def test_job_falls_back_to_faster_whisper_then_cpu() -> None:
    job = load_job()
    calls: list[tuple[str, str]] = []

    def wx(src: str, device: str, ctype: str, model: str) -> tuple[list[Any], str]:
        calls.append(("whisperx", device))
        raise ImportError("No module named 'whisperx'")

    def fw(src: str, device: str, ctype: str, model: str) -> tuple[list[Any], str]:
        calls.append(("faster-whisper", device))
        if device == "cuda":
            raise RuntimeError("CUDA failed with error out of memory")
        assert ctype == "int8"
        return seg("ok"), "en"

    engines = [("whisperx", wx), ("faster-whisper", fw)]
    out = job.transcribe("in.mov", "cuda", "float16", engines=engines)
    assert calls == [
        ("whisperx", "cuda"),
        ("faster-whisper", "cuda"),
        ("whisperx", "cpu"),
        ("faster-whisper", "cpu"),
    ]
    assert (out["engine"], out["device"]) == ("faster-whisper", "cpu")


def test_job_main_writes_json_or_fails_without_output(tmp_path: Path) -> None:
    job = load_job()
    out = tmp_path / "rw-transcript-1.json"

    def ok(src: str, device: str, ctype: str, model: str) -> tuple[list[Any], str]:
        return seg("yo"), "en"

    def broken(src: str, device: str, ctype: str, model: str) -> tuple[list[Any], str]:
        raise OSError("cannot read media")

    argv = ["--input", "in.mov", "--output", str(out), "--device", "cpu", "--compute-type", "int8"]
    assert job.main(argv, engines=[("broken", broken)]) == 1
    assert not out.exists()
    assert job.main(argv, engines=[("ok", ok)]) == 0
    assert json.loads(out.read_text())["words"][0]["word"] == "yo"


def test_executors_run_the_job_script_on_cuda_or_cpu() -> None:
    payload = {"input": "in.mov", "output": "rw-transcript-1.json"}
    gpu_argv = GPU_EXECUTORS[JobKind.transcribe](payload)
    cpu_argv = CPU_EXECUTORS[JobKind.transcribe](payload)
    assert str(TRANSCRIBE_JOB) in gpu_argv and TRANSCRIBE_JOB.is_file()
    assert gpu_argv[gpu_argv.index("--device") + 1] == "cuda"
    assert cpu_argv[cpu_argv.index("--device") + 1] == "cpu"
    assert cpu_argv[cpu_argv.index("--compute-type") + 1] == "int8"


# --- R4/R8: every camera and screen asset, through the picker -----------------------------------


def test_manifest_queues_transcribe_for_camera_and_screen_only(client: TestClient) -> None:
    entries = [
        {**SCREEN, "kind": kind, "path": f"D:\\m\\{kind}.mp4", "checksum": c * 64}
        for kind, c in (("camera", "1"), ("screen", "2"), ("broll", "3"), ("audio", "4"))
    ]
    assets = client.post("/api/assets/manifest", json={"entries": entries}).json()
    by_kind = {a["kind"]: a for a in assets}
    assert by_kind["broll"]["transcript_job_id"] is None
    assert by_kind["audio"]["transcript_job_id"] is None
    jobs = {j.id: j for j in jobs_of(JobKind.transcribe)}
    assert set(jobs) == {
        by_kind["camera"]["transcript_job_id"],
        by_kind["screen"]["transcript_job_id"],
    }
    screen = by_kind["screen"]
    read = queue.job_to_read(jobs[screen["transcript_job_id"]])
    assert read.required_caps == ["cuda"] and read.worker is None
    assert read.min_vram_mb == transcript.TRANSCRIBE_MIN_VRAM_MB
    assert read.payload == {
        "asset_id": screen["id"],
        "input": "D:\\m\\screen.mp4",
        "output": f"rw-transcript-{screen['id']}.json",
        "upload": f"/api/assets/{screen['id']}/transcript",
        "content_type": "application/json",
    }
    assert screen["proxy_job_id"] < screen["transcript_job_id"]  # the proxy stays first

    again = client.post("/api/assets/manifest", json={"entries": entries}).json()
    assert [a["transcript_job_id"] for a in again] == [a["transcript_job_id"] for a in assets]
    assert len(jobs_of(JobKind.transcribe)) == 2


def claim(client: TestClient, worker: str) -> dict[str, Any] | None:
    r = client.get("/api/workers/next", params={"worker": worker})
    assert r.status_code in (200, 204), r.text
    return r.json() if r.status_code == 200 else None


def finish(client: TestClient, job: dict[str, Any], worker: str) -> None:
    body = {"worker": worker, "ok": True, "outputs": {}}
    assert client.post(f"/api/workers/jobs/{job['id']}/complete", json=body).status_code == 200


def test_transcribe_runs_on_a_free_gpu_before_the_cpu(client: TestClient) -> None:
    client.post("/api/workers/heartbeat", json=CPU)
    client.post("/api/workers/heartbeat", json=GPU)
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    proxy_job = claim(client, GPU["name"])
    assert proxy_job is not None and proxy_job["kind"] == "proxy"
    assert claim(client, "ci-cpu") is None  # a busy GPU makes the job wait, not go to CPU
    finish(client, proxy_job, GPU["name"])
    job = claim(client, GPU["name"])
    assert job is not None and job["id"] == asset["transcript_job_id"]
    assert job["kind"] == "transcribe"


def test_transcribe_falls_back_to_a_cpu_worker_without_gpus(client: TestClient) -> None:
    client.post("/api/workers/heartbeat", json=CPU)
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    proxy_job = claim(client, "ci-cpu")
    assert proxy_job is not None
    finish(client, proxy_job, "ci-cpu")
    job = claim(client, "ci-cpu")
    assert job is not None and job["id"] == asset["transcript_job_id"]


# --- R3/R5/R6: end to end through the agent, the asset view and the guards ----------------------


def test_cpu_agent_transcribes_and_asset_view_shows_words(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "proxies"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", "0")
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    view = client.get(f"/api/assets/{asset['id']}")
    assert view.status_code == 200 and view.json()["transcript"] is None
    monkeypatch.chdir(tmp_path)
    ran: list[list[str]] = []

    def fake_runner(argv: list[str], policy: ResourcePolicy) -> int:
        ran.append(argv)
        assert policy.nice  # low priority on the owner's work PC
        if str(TRANSCRIBE_JOB) in argv:
            out = argv[argv.index("--output") + 1]
            (tmp_path / out).write_text(json.dumps({**UPLOAD, "device": "cpu", "words": WORDS}))
        else:
            (tmp_path / argv[-1]).write_bytes(b"720p-proxy")
        return 0

    agent = Agent(
        "http://127.0.0.1:8000",
        "ci-cpu",
        cpu=True,
        client=client,
        runner=fake_runner,
        probe=cpu_probe,
        policy=ResourcePolicy(mem_cap_mb=4096),
    )
    assert agent.tick() is True  # proxy
    assert agent.tick() is True  # transcript
    assert agent.tick() is False
    assert ran[1][ran[1].index("--device") + 1] == "cpu"
    with Session(get_engine()) as session:
        job = session.get(RenderJob, asset["transcript_job_id"])
        assert job is not None and job.status == "done"
        assert queue.job_to_read(job).outputs["uploaded"] is True
    shown = client.get(f"/api/assets/{asset['id']}").json()
    assert shown["transcript_id"] == shown["transcript"]["id"]
    assert shown["transcript"]["words"] == WORDS
    assert shown["transcript"]["device"] == "cpu"
    assert not (tmp_path / f"rw-transcript-{asset['id']}.json").exists()


def test_asset_view_and_upload_404_for_unknown_asset(client: TestClient) -> None:
    assert client.get("/api/assets/999").status_code == 404
    assert client.put("/api/assets/999/transcript", json={**UPLOAD, "words": []}).status_code == 404


def test_transcript_upload_needs_worker_token_when_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    monkeypatch.setenv("REELWRIGHT_WORKER_TOKEN", "t0ken-for-test")
    url = f"/api/assets/{asset['id']}/transcript"
    assert client.put(url, json={**UPLOAD, "words": WORDS}).status_code == 401
    good = {"Authorization": "Bearer t0ken-for-test"}
    assert client.put(url, json={**UPLOAD, "words": WORDS}, headers=good).status_code == 200


def test_transcript_upload_refused_below_floor_or_over_size(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REELWRIGHT_PROXY_DIR", str(tmp_path / "proxies"))
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", str(10**12))  # never enough free disk
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    url = f"/api/assets/{asset['id']}/transcript"
    r = client.put(url, json={**UPLOAD, "words": WORDS})
    assert r.status_code == 507 and "floor" in r.json()["detail"]
    monkeypatch.setenv("REELWRIGHT_FREE_DISK_FLOOR_MB", "0")
    monkeypatch.setattr(transcript, "MAX_TRANSCRIPT_BYTES", 64)
    assert client.put(url, json={**UPLOAD, "words": WORDS}).status_code == 413
    assert client.get(f"/api/assets/{asset['id']}").json()["transcript"] is None


def test_replaced_file_drops_its_transcript_and_queues_a_new_one(client: TestClient) -> None:
    (asset,) = client.post("/api/assets/manifest", json={"entries": [SCREEN]}).json()
    url = f"/api/assets/{asset['id']}/transcript"
    assert client.put(url, json={**UPLOAD, "words": WORDS}).status_code == 200
    (replaced,) = client.post(
        "/api/assets/manifest", json={"entries": [{**SCREEN, "checksum": "f" * 64}]}
    ).json()
    assert replaced["id"] == asset["id"] and replaced["transcript_id"] is None
    assert replaced["transcript_job_id"] not in (None, asset["transcript_job_id"])
    assert client.get(f"/api/assets/{asset['id']}").json()["transcript"] is None
