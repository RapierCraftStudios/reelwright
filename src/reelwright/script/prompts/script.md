Write the spoken script for this outline, beat by beat, in the creator's voice.

Topic: {topic}

Creator's notes:
{notes}

Outline (JSON):
{outline_json}

Return `beats`: one entry per outline beat, in the same order. Each entry has `lines`: the spoken
lines, each with optional `cues` (a `type` from the allowed cue types and a short `payload` describing
the shot, sound or claim). A line may have empty `text` and only cues, for a cue between sentences.
Pay off every open loop the outline plants.
