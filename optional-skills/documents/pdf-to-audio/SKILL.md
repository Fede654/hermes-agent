---
name: pdf-to-audio
description: "Narrate a PDF / book chapter to audio (optionally translated), the FULL text not a fragment. Covers reliable PDF extraction (PyMuPDF, all pages), per-language Kokoro voice, and Telegram Opus voice bubbles. Reads from the Research Library corpus when the book is already acquired."
version: 1.1.0
author: Chiwa
metadata:
  hermes:
    tags: [PDF, audio, TTS, narration, translation, Kokoro, Telegram, voice]
    related_skills: [library-acquisition]
---

# PDF / chapter → audio (full text, optionally translated)

## When to use
The user uploads a PDF (or points to one) and asks to translate and/or narrate
it to audio. The failure mode this prevents: voicing only a FRAGMENT of the
chapter because the PDF text was never fully extracted.

## Steps

0. **If the book is already in the Research Library, read the chapter, don't
   re-extract.** Check `$LIBRARY_ROOT/topics/**/books/*/meta.json`; if present,
   read the chapter `.txt` from `chapters/` directly. (Acquire new books with the
   `library-acquisition` skill.)

1. **Otherwise extract the FULL text — do NOT use `read_file` (it can't parse
   PDFs) and do NOT `import fitz` inside `execute_code` (its sandbox lacks
   PyMuPDF).** Use the `terminal` tool with the consolidated extractor in `dump`
   mode, via a python that has PyMuPDF:
   ```
   ./scripts/hmk library_extract.py dump "<pdf-path>" --out /tmp/chapter.txt
   ```
   It prints `pages=N chars=M` to stderr — **confirm M matches a full chapter**
   (a chapter is usually 15k–40k chars; a few-hundred/thousand means you only got
   a fragment — investigate before continuing). Uploaded PDFs live under
   `hermes-home/cache/documents/`.

2. **Translate** (if asked) with the model — translate the WHOLE extracted text,
   not a summary. Keep paragraph structure.

3. **Synthesize with `text_to_speech`.** Pick the `voice` for the language
   (ES `em_alex`, EN-UK `bm_lewis`, IT `im_nicola`, PT `pm_alex`, …) — see the
   voice catalogue in the tool schema.
   - On the **local self-hosted Kokoro** server the tool no longer truncates
     (the per-provider cap is lifted for non-`api.openai.com` endpoints), so a
     full chapter can go in **one call**.
   - Only chunk (~3000–3500 chars on paragraph boundaries) when targeting the
     **real `api.openai.com`** (hard 4096 cap) or when a single request is
     impractically large.

4. **Deliver.** On **Telegram** the tool always emits **Opus `.ogg`** → native
   voice bubbles. (On other platforms an mp3 file is fine.) Very large mp3s can
   time out the Telegram upload — segment with `ffmpeg -f segment -segment_time
   180` and send one part at a time.

## Gotchas
- `chars=` far below the chapter's real size ⇒ extraction problem, not a TTS
  limit — re-extract, don't ship a 1/5 chapter.
- Don't name output files `.mp3` expecting a voice bubble; on Telegram the tool
  coerces to `.ogg` anyway, but keep names extension-agnostic.
- One PDF extractor only: `library_extract.py` (`dump` here, `corpus` for the
  Research Library). Don't reintroduce ad-hoc extract scripts.
