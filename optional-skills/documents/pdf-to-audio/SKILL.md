---
name: pdf-to-audio
description: "Dictate/narrate text or a PDF/book chapter to audio (optionally translated), the FULL text. ONE step: call the text_to_speech tool with the whole text — it generates AND delivers the voice bubble automatically. No direct API, no send_message, no chunking, no ffmpeg."
version: 1.2.0
author: Chiwa
metadata:
  hermes:
    tags: [PDF, audio, TTS, narration, translation, Kokoro, Telegram, voice]
    related_skills: [library-acquisition]
---

# Dictate text / a chapter to audio

## The ONE correct path (trust the tool — no workarounds)

1. **Get the full text.**
   - Plain text / a message: use it as-is.
   - Already in the Research Library: read the chapter `.txt` from `$LIBRARY_ROOT/topics/**/books/*/chapters/`.
   - A PDF: extract ALL of it with the consolidated extractor (PyMuPDF; `execute_code`'s sandbox lacks it, so use the `terminal` tool):
     ```
     ./scripts/hmk library_extract.py dump "<pdf-path>" --out /tmp/chapter.txt
     ```
     Confirm `chars=` matches a full chapter (15k–40k typical); a tiny count means a bad extraction — re-extract, don't ship a fragment.

2. **Translate if asked** — the WHOLE text, preserving paragraphs (not a summary).

3. **Speak it — ONE call to `text_to_speech` with the FULL text:**
   ```
   text_to_speech(text="<entire chapter>", voice="<voice for the language>")
   ```
   - The local Kokoro server has **no length limit** — pass the whole chapter; it is **not truncated**.
   - The tool returns a `MEDIA:` voice tag that the gateway **delivers to the chat automatically** as a voice bubble. You are done.
   - Voice by language: ES `em_alex`/`ef_dora`, EN-UK `bm_lewis`, EN-US `am_adam`, PT `pm_alex`, IT `im_nicola`, FR `ff_siwis`, … (see the tool's `voice` catalogue).

## Do NOT (these are the old broken workarounds)
- ❌ Do NOT call the Kokoro HTTP API (`/v1/audio/speech`) directly.
- ❌ Do NOT call `send_message` to deliver the audio — `text_to_speech` already delivers it.
- ❌ Do NOT chunk the text under any "4096" limit — that cap does not apply to the local server.
- ❌ Do NOT split/segment the audio with `ffmpeg`. The Telegram upload timeout is configured high enough (180s) for a full chapter; one voice bubble is correct.

If a single delivery ever genuinely fails, report the exact tool error — do not silently fall back to a workaround.
