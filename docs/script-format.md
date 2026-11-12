# Script format

A Reelwright script is one markdown file per video. It is the source of truth: takes, edits, clips and
metrics all point back to its beats (spike § Data model). The parser lives in `src/reelwright/script/` and
is the only implementation of this grammar; the dashboard editor highlights through
`POST /api/projects/:id/script/parse`.

## Front matter

The file starts with YAML between two `---` lines.

| Key | Required | Meaning |
|-----|----------|---------|
| `project` | no | project slug |
| `format` | yes | `long`, `short` or `vlog` |
| `working_title` | yes | the title while writing |
| `title_options` | no | list of candidate titles |
| `thumbnail_ideas` | no | list of thumbnail ideas |
| `target_length` | no | free text, for example `8-10 min` |
| `status` | no | defaults to `draft` |

Any other key is an error.

## Beats

Each beat is an H2 heading:

```
## beat: <slug> | intent: <free text>
```

The slug is lowercase letters, digits and dashes, unique in the script, and is the stable key other modules
point to. The beat type is the slug's first word (`point-2` is a `point` beat). Any other `## ` heading is
an error, and so is content before the first beat.

## Cues

A cue is `[type: payload]`, alone on a line or attached to a spoken line (its position in the line is
kept). The payload may be empty (`[cut:]`).

| Type | Meaning |
|------|---------|
| `broll` | b-roll footage |
| `anim` | a motion-graphics scene |
| `screen` | a screen recording |
| `fact` | a claim to fact-check and cite |
| `sfx` | a sound effect |
| `music` | a music change |
| `cut` | an edit instruction (jump cut, punch-in) |
| `short` | a span to cut as a Short |

Any other `[word: …]` is an error naming the line and column, for example
`line 12, col 5: unknown cue type 'zoom'`.

## Ad-libs, placeholders and notes

- `{curly}` marks an ad-lib or fill-in. It is spoken and kept in the line, but not aligned strictly.
- `[TODO …]` marks a placeholder. Drafts may contain them; locking is refused while any remain.
- `<!-- … -->` (one or many lines) is an author note, ignored by the parser and the prompter.

## Spoken lines

Every other non-empty line is a spoken line. Each spoken line is one alignment unit for best-take
selection; cue tags are removed from its text.

## Versions and lock

`PUT /api/projects/:id/script` stores a new version (422 with line-numbered errors if it does not parse).
`POST /api/projects/:id/script/lock` freezes the latest version; it is refused (409) while any `[TODO …]`
remains, and a locked script refuses new versions (409).

## Example

```markdown
---
project: first-video
format: long
working_title: Why I build in public
title_options:
  - I quit my job to build in public
thumbnail_ideas:
  - me pointing at a burning org chart
target_length: 8-10 min
status: draft
---

## beat: hook | intent: open loop on the resignation email
[music: low pulse]
A year ago I hit send on an email I'd rewritten nine times. [screen: the email, blurred]
{quick aside about the coffee}

## beat: setup | intent: why it was a risk
<!-- check the figure before recording -->
I had a mortgage and two kids. [fact: median staff engineer salary]
[broll: office desk] [anim: salary counter] [sfx: whoosh]

## beat: cta | intent: subscribe for the money video
The next one is about the money. [cut: punch-in] [short: last two sentences]
```
