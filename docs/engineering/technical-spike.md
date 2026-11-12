# Technical Spike: Reelwright

> Written by /bootstrap on 2026-11-12. Every decision has a `**Rationale**:` line giving the decision, why, and the rejected
> alternative.

Reelwright is an AI-run creator studio for **Yash Dubey (@itsMrDubey)**. The creator records: a talking head on the DJI Osmo
Action 5 Pro, screen takes, lifestyle b-roll. Everything else is automated: research, script, teleprompter, edit, motion
graphics, music, render, Shorts, publishing, analytics and community. It is public and AGPL so other creators can use
it, but the primary goal is to grow this one channel to a primary income in about three months.

## Assumptions

- **Users**: one operator (Yash) and agents acting for him. No other end users. Other creators self-host their own copy.
- **Core jobs on day one**: write a script, read it on the teleprompter, drop footage, get a rough cut, upload. Everything
  else is added as videos need it.
- **Scope**: MVP that ships video #1 in about 2 weeks. The rest ships in milestone order, one piece per video
  (the cheapest path to feedback).
- **Platforms**: a web dashboard and teleprompter served over Tailscale (works on phone and desktop with one codebase).
  The capture app is the Recordly desktop fork on Windows.
- **Stack**: the Python API preset (uv, FastAPI), plus TypeScript for the web, motion and capture. SpectraCut uses
  the same, and the shared code is Python.
- **Auth**: no login. The service binds only to the Tailscale interface. It is single-tenant and single-user.
- **Data and tenancy**: single tenant. SQLite for metadata. Media stays on the capture PC.
- **Integrations**: a mock provider for every credentialed service (YouTube, AlterLab, music library), so dev and CI
  stay green. X, Instagram, Facebook and TikTok are driven through **AlterLab Hands** (the creator's own logged-in
  sessions), not paid platform APIs.
- **Payments**: none in the product. Income comes from YouTube Partner Program (YPP), affiliate links, sponsorships and the AlterLab funnel.
- **i18n**: English only. Captions can be translated later.
- **Compliance**: platform terms (YouTube API, Meta, X, TikTok), music licensing, sponsorship disclosure rules. Minimal PII
  (commenter handles only).
- **Design**: the motion library is the channel's visual identity (hero, routed to `/design`). The dashboard is
  functional.
- **Deployment**: systemd user services on the Arch dev server, plus GPU workers on MrDubey and Legion.
- **Scale**: one channel, about 1 long video and 5 Shorts a week, roughly 50 to 200 GB of footage a month.
- **Budget**: free or hobby tiers. No paid platform APIs: social posting goes through AlterLab Hands. The only paid
  item is a music library subscription.
- **Team**: Yash plus ForgeDock agents.
- **Timeline**: video #1 by about 2026-11-26. YPP-eligible and earning by about 2027-02-12.

## Guardrails

- **Nothing is published or sent without explicit approval.** Uploads, posts, comment replies and sponsor emails are
  drafted and queued. A human approves each one in the dashboard. Scheduled publishing only runs items already approved.
- **No secrets in the repo, issues, logs, scripts or renders.** Credentials live in an untracked `.env` or the
  keychain only.
- **Raw footage never lands on the dev server.** Its disk is 95% full (27 GB free). Only proxies, transcripts, audio
  stems and small assets are stored there, with a disk-budget check before every write.
- **MrDubey is the owner's work PC.** Never resize, unpause or add lanes on MrDubey through computeshare. GPU jobs there
  run at low CPU priority with a RAM cap, and yield when a full-screen app or a heavy GPU process is running.
- **Only licensed or self-made music and SFX**: library tracks with a valid licence, or original stems. No AI-generated
  music in final videos (Content ID and copyright risk). No copyrighted footage without a fair-use note on the cue.
- **Facts in scripts carry a source.** Claims marked `[fact]` must have an AlterLab-fetched citation before the script
  is locked.
- **Sponsorship and affiliate disclosure is automatic.** Any video or post with a sponsor or affiliate link gets the
  platform's paid-promotion flag and a spoken or on-screen disclosure.
- **Every automated edit is reversible.** Each cut also exports OTIO, so a human can finish it in an NLE.

## Channel strategy

This section drives the product priorities. It is not code.

- **Position.** A builder's life: shipping real AI tools (AlterLab, ForgeDock, SpectraCut, Reelwright itself) mixed
  with the life around it (moving into the apartment, gym, friends, podcasts). It's the tech-plus-lifestyle blend
  PewDiePie does now, with build-in-public as the spine. The edge is that the channel *is* the demo: every video is
  edited by the tools it talks about.
- **The YPP gate is watch hours, not subscribers.** It needs 1,000 subscribers plus either 4,000 public watch hours in
  12 months or 10M Shorts views in 90 days. Long-form videos (12 to 20 minutes, strong retention) earn the hours.
  Shorts earn subscribers.
- **Cadence.** 1 long video a week, plus 4 to 6 Shorts a week cut from it and from daily footage, starting with
  video #1.
- **Income in three months is a mix, not ad revenue alone:**
  - **AlterLab funnel**: tracked links and on-screen demos in relevant videos.
  - **Affiliate links**: gear and tools, from video #1.
  - **YPP**: once eligible.
  - **Sponsorships**: from about 5,000 to 10,000 subscribers.
- **Learning loop.** Each video's retention curve maps back to its script beats. Beat types that hold viewers get
  reused; ones that lose them get cut. This is the main way to improve faster than channels that edit by hand.

## Stack

- **Pipeline and API**: Python 3.12, uv, FastAPI, Pydantic v2, SQLModel on SQLite, Typer CLI.
- **Speech**: WhisperX (word timestamps and diarization) on a GPU worker, with faster-whisper as fallback. A small
  streaming Whisper model drives the teleprompter.
- **Edit model**: OpenTimelineIO, through SpectraCut's `spectracut.edit` (timeline and OTIO I/O), pinned to a commit.
- **Motion graphics engine**: **HTML/CSS/SVG scenes using the Web Animations API, rendered frame by frame by headless
  Chromium on a virtual clock (Playwright), then encoded with ffmpeg.** Scenes are plain web code with a `seek(t)`
  contract. The house style lives in design tokens and a component library under `packages/motion`.
- **Render**: ffmpeg with NVENC (h264, hevc, av1 on the 4070 Ti) on the GPU workers.
- **Web**: TypeScript, Vite, React, Tailwind. One app holds the dashboard and teleprompter routes, served by FastAPI.
- **Capture**: a fork of Recordly (Electron, TypeScript, AGPL) with a thin recorder patch. Visual add-ons use
  Recordly's own extension API.
- **LLM**: SpectraCut's `providers` layer (Claude Code provider on existing subscriptions, mock for CI).
- **Facts and research**: AlterLab API (scrape and extract) and the YouTube Data API.
- **Social publishing and engagement**: AlterLab Hands browser tasks on the creator's connected sessions
  (`alterlab_task_start`, `alterlab_task_act` with `upload`, `type` and `click`, `alterlab_task_answer` for approvals)
  for X, Instagram, Facebook and TikTok. No paid platform APIs.

**Rationale (publishing)**: the X API's paid tier and Meta/TikTok app reviews cost money and weeks. Hands acts as the
creator in their own logged-in browser session, with write actions gated by approval. Rejected: official APIs for
these four platforms.

**Rationale**:
- **Python core**: the speech, OTIO and SpectraCut pieces are Python, so the pipeline is too.
- **Motion engine**: HTML scenes are what an LLM writes most fluently. That means the fewest tokens per scene and the
  best first-try quality. They lock us into nothing (it's the web platform), and they share the virtual-clock capture
  idea in SpectraCut's live-UI capture. Rejected alternatives:
  - Remotion: its licence is not OSI-approved, and companies of 4+ people must pay. That's wrong for a public AGPL
    tool other creators adopt.
  - Motion Canvas: MIT, but LLMs write it less fluently.
  - After Effects / Resolve Fusion: closed and not scriptable headlessly.
- **Edit model**: an OTIO-native model, not a custom timeline. The hand-off to Resolve, Premiere or Kdenlive comes free.

## Repo layout

```
reelwright/
  pyproject.toml, uv.lock, package.json, pnpm-workspace.yaml, forge.yaml, LICENSE (AGPL-3.0-or-later)
  src/reelwright/
    api/            FastAPI app: routes, SSE events, static web bundle
    db/             SQLModel models, migrations (Alembic)
    script/         script format (parser, cue tags), LLM writer, fact check
    prompter/       live alignment service (streaming ASR → script position)
    ingest/         watcher client, sync, proxy, transcription jobs
    edit/           take alignment, best-take picker, silence/fumble cut, b-roll + cue placement, OTIO export
    captions/       word-timed caption tracks, styling, burn-in
    audio/          music/SFX selection, ducking, loudness (EBU R128), cleanup
    motion/         scene spec → render job; Python side of the motion engine
    render/         job queue, GPU worker registry and picker, ffmpeg graphs
    clips/          moment scoring, 9:16 reframing (face tracking), Shorts packaging
    publish/        platform adapters (youtube, x, instagram, facebook, tiktok) + approval queue
    analytics/      YouTube Analytics ingest, retention → beat mapping, reports
    research/       outlier miner, topic + title ideation
    community/      comment ingest, triage, reply drafts
    business/       affiliate links, AlterLab UTM funnel, sponsor CRM
    providers/      mock + real providers for every external service
    cli/            `rw` Typer CLI
  workers/gpu/      GPU worker agent (Windows + WSL): ffmpeg NVENC, Chromium frame capture, WhisperX
  workers/watcher/  footage watcher on the capture PC (Windows)
  apps/web/         Vite + React: dashboard + /prompter
  packages/motion/  scene runtime (seek contract), design tokens, component library, scene templates
  packages/recordly-ext/  Recordly extensions (house cursor, frames, zoom look)
  capture/recordly/ git submodule → RapierCraftStudios/recordly (fork)
  deploy/           systemd units, deploy script, Tailscale serve config
  docs/engineering/ technical-spike.md, ADRs
  docs/setup/       per-credential setup guides
  tests/            pytest (unit, api), e2e (Playwright)
```

**Rationale**: one repo for the pipeline, web, motion and workers keeps the cue-tag contract in one place and lets
one PR change producer and consumer together. Recordly stays a separate fork (a submodule) because it is a large
upstream Electron app, and rebasing it is easier on its own. Rejected: a polyrepo per service, which is too much
coordination for one creator.

## Runtime topology

```
 phone (prompter)  browser (dashboard)            Windows: MrDubey (RTX 4070 Ti 12 GB, footage)
        │                │                          ├─ watcher: DJI/Recordly folders → manifest + checksum
        └──Tailscale─────┤                          ├─ gpu-worker (session agent, low priority, RAM cap)
                         ▼                          └─ Recordly fork (capture + event log)
     Arch dev server (no GPU)                     Windows: Legion (RTX 3070 Laptop 8 GB, AC power only)
       reelwright-api (FastAPI, SQLite, SSE)        └─ gpu-worker (in WSL forge-runner or native)
       reelwright-scheduler (jobs, publish queue)
       proxies/transcripts (disk-budgeted)
                         │
          picks a GPU worker per job: free card, enough VRAM, required caps (nvenc/cuda), not
          yielding; media read over the tailnet from MrDubey (SMB share), outputs written back to MrDubey
```

- **Day one**: one API process and one scheduler on the dev server. GPU workers register themselves with a heartbeat
  that reports GPU model, VRAM, utilisation, temperature, a busy/yield flag and capabilities. The picker sends each job
  to the best free card. It splits final renders by segment across both cards when both are free.
- **Next**: when computeshare ships whole-GPU leases (SpectraCut spike, "computeshare GPUs"), the picker becomes a
  computeshare client behind the same interface.
- **At scale (other creators)**: the same services in Docker Compose on one machine with a local GPU.
- **The teleprompter** runs in the phone browser. Mic audio streams over WebSocket to the prompter alignment service,
  which runs a small Whisper model on the dev server's CPU (or a GPU worker when one is free). Position comes back at
  under 300 ms latency.

**Rationale**: footage is created on MrDubey and is too large for the dev server, so compute goes to where the media
and GPUs are. The control plane stays on the always-on Arch box. A worker heartbeat plus a picker is the smallest
thing that uses "whatever GPU is free" today, without waiting for computeshare GPU leases. Rejected:
- copying footage to the dev server: there isn't room;
- a cloud GPU: the owner's two cards are free and already on the tailnet.

## Data model

- **Project**: one video. Fields: slug, title, format (long, short, vlog), status, target publish date.
- **Script**: versioned markdown with cue tags. A Script has ordered **Beats**: hook, setup, point, payoff, CTA, and so
  on, each with a type, intent and cues. **Cues** are typed: `[broll:]`, `[anim:]`, `[screen:]`, `[fact:]`, `[sfx:]`,
  `[music:]`, `[cut:]`, `[short:]`.
- **Asset**: a source file on MrDubey. Fields: path, checksum, kind (camera, screen, broll, audio, music), duration,
  proxy path, transcript id, and Recordly event log if present.
- **Take**: a span of an Asset aligned to a Beat or script line, with quality scores (fluency, energy, audio, framing).
- **Edit**: an OTIO timeline (spectracut.edit) plus a version.
- **RenderJob**: kind, worker, status, outputs.
- **Clip**: a source span, virality score with reasons, a 9:16 render, and the platform variants.
- **Publication**: platform, status (draft → approved → scheduled → live), remote id, approval record.
- **Metric**: publication, date, views, watch time, CTR, and the retention curve stored as a JSON array.
- **BeatInsight**: beat type → retention delta, learned across videos.
- **Comment**: platform, author handle, text, triage label, draft reply, approval.
- **Sponsor/Affiliate**: brand, link, UTM, disclosure requirement, deal status.

Tenancy: single. Retention:
- proxies are deleted 30 days after publish;
- transcripts, edits and metrics are kept forever (they are the learning data);
- raw footage is the creator's, on MrDubey.

**Rationale**: the Script and Beat are the source of truth. Takes, edits, clips and metrics all point back to beats,
which is what makes the retention-to-beat learning loop possible. Rejected: a free-form timeline as the source of
truth, because analytics couldn't be mapped back to intent.

## Security and privacy

- **Authn/authz**: the API binds only to the Tailscale IP. Tailscale ACLs limit access to Yash's devices. GPU workers
  authenticate with per-worker bearer tokens issued at enrolment.
- **Secrets**: platform OAuth tokens and API keys are stored in the system keychain (`secret-tool`), loaded at start,
  never written to SQLite or logs. OAuth refresh happens server-side.
- **PII**: only commenter handles and comment text, used for triage, and purged after 180 days.
- **Approval records**: each publish or send stores who approved it, when, and the exact payload hash.
- **Compliance**: each platform's API terms, YouTube's paid-promotion flag, FTC/ASCI disclosure for sponsors and
  affiliates, and the licence-tracking guardrails above.

**Rationale**: a single-user tool on a private tailnet needs no login system, and adding one would only slow the
creator down. Network isolation plus keychain secrets covers the real risk, which is leaking platform tokens. Rejected:
public hosting with auth, an attack surface with no user.

## Design system and quality gates

- **Channel look (hero)**: design tokens in `packages/motion/tokens` (type scale, colour roles, motion curves, timing
  grid) shared by motion scenes, captions, thumbnails and Recordly extensions. Routed through `/design`. The goal is a
  distinctive identity, not a template look.
- **Dashboard**: shadcn/ui and Tailwind, dark-first, WCAG 2.2 AA.
- **Teleprompter**: large high-contrast type, mirror mode, reduced-motion fallback.
- **Media quality gates**, as an automated QA job before any publish:
  - loudness at -14 LUFS integrated (YouTube target), true peak at most -1 dBTP;
  - no black or frozen frames;
  - captions inside safe areas and readable at phone size;
  - no silence over 1.5 s that isn't deliberate;
  - a final render → critique → revise pass (SpectraCut `critique`).
- **CI gates**: ruff, mypy, pytest; tsc, ESLint, Vitest; a Playwright smoke test of the dashboard and prompter; golden-
  frame tests for motion components.

**Rationale**: the look is the channel's moat, so it gets tokens and golden-frame tests like code does. Media QA catches
the defects viewers punish (loudness jumps, dead air) before they ship. Rejected: per-video hand styling, which drifts
and can't be automated.

## Route/API map

| Route or endpoint | Purpose | Auth |
|-------------------|---------|------|
| `GET /` | Dashboard: projects, pipeline status, approval queue | tailnet |
| `GET /prompter/:project` | Teleprompter view (phone) | tailnet |
| `WS /api/prompter/:project/align` | Mic audio in, script position out | tailnet |
| `/api/projects` CRUD | Projects | tailnet |
| `/api/projects/:id/script` | Get/put/generate script, lock, fact-check | tailnet |
| `/api/projects/:id/takes` | Camera take markers from the prompter (beat, line range, start time) | tailnet |
| `POST /api/assets/manifest` | Watcher reports new footage | worker token |
| `/api/projects/:id/edit` | Build rough cut, get OTIO, request render | tailnet |
| `/api/jobs` | Job queue status (SSE `/api/events`) | tailnet |
| `POST /api/workers/heartbeat`, `GET /api/workers/next` | GPU worker registration and job pull | worker token |
| `/api/clips` | Candidate Shorts, scores, approve | tailnet |
| `/api/publish` | Drafts, approve, schedule per platform | tailnet |
| `/api/analytics/:project` | Retention by beat, channel KPIs | tailnet |
| `/api/research` | Outliers, topic ideas | tailnet |
| `/api/community` | Comment triage and reply drafts | tailnet |
| `GET /healthz` | Health | none (tailnet) |

**Rationale**: one REST surface with SSE for job progress is enough for one operator. Workers pull jobs, so Windows
hosts need no inbound port beyond the tailnet. Rejected: GraphQL or gRPC, which is complexity with no consumer to
justify it.

## Credentials

| Credential or account | Env var(s) | needs-human issue | Mock in dev/CI |
|-----------------------|------------|-------------------|----------------|
| Google Cloud OAuth client (YouTube Data + Analytics API) | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` | YouTube API OAuth client | `MockYouTube` (records uploads, canned analytics) |
| AlterLab Hands sessions for X, Instagram, Facebook and TikTok (creator signs in once per site via `alterlab_connect`; Reelwright never sees cookies or passwords) | `ALTERLAB_API_KEY` (scopes `hands:read`, `hands:write`) | Connect social accounts to AlterLab Hands | `MockHands` (records task actions) |
| Music/SFX library subscription with API or bulk export | `MUSIC_LIBRARY_API_KEY` | Licensed music library | `MockMusic` (bundled CC0 test tracks) |
| AlterLab API key (scrape, `hands:read`, `hands:write`) | `ALTERLAB_API_KEY` | AlterLab key for Reelwright | `MockAlterLab` (fixture pages) |
| GPU worker enrolment on MrDubey and Legion (Windows admin steps) | `REELWRIGHT_WORKER_TOKEN` | Install GPU workers | `LocalCpuWorker` (CPU ffmpeg) |
| Teleprompter hardware (beam splitter + phone mount) | — | Teleprompter hardware | phone-above-lens mode |

**Rationale**: every platform account, paid tier and admin install needs a human, and some need days for app review
(Meta, TikTok). So each is a needs-human issue gating only real publishing, while the mock keeps the build moving. YouTube
uses its free official API (uploads, analytics). Every other platform runs through AlterLab Hands, which avoids paid
API tiers and app reviews. Hands' own approval step for write actions sits on top of Reelwright's approval queue.

## Milestone plan

1. **M1 First video** (about 2026-11-26): the skeleton (API, dashboard, CLI, CI, deploy), script format and writer,
   voice-following teleprompter, footage watcher and ingest with proxies and WhisperX, GPU worker and picker,
   best-take rough cut with silence/fumble removal and OTIO export, captions, loudness QA, and YouTube upload behind
   approval.
2. **M2 Look and Shorts** (about 2026-12-10): motion engine and house component library (hero), cue-driven b-roll
   and animation placement, music and SFX with ducking, Shorts extraction with 9:16 reframing, multi-platform
   publishing, thumbnail generator, render critique loop.
3. **M3 Growth loop** (about 2027-01-07): YouTube Analytics ingest, retention-to-beat mapping that feeds the script
   writer, outlier and topic research, AlterLab fact check, Recordly fork with event log and auto-zoom, community
   triage.
4. **M4 Business and release** (about 2027-02-04): AlterLab funnel tracking, affiliate manager, sponsor CRM and
   disclosure, docs and a one-command install for other creators.

**Rationale**: build order follows the next video's needs, not architecture purity. M1 is exactly what video #1
requires. M2 adds the look and the Shorts engine that drive subscribers. M3 closes the learning loop once there is data
to learn from (a few published videos). M4 adds income levers that only matter once there is an audience. Deploy is
in M1, so every later milestone runs live as it lands.
