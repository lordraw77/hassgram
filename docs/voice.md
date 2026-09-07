# Voice commands

Send a voice note with the same command you would type. The bot transcribes it
with the speech-to-text engine **already configured inside Home Assistant** —
no extra service, no extra API key, no ffmpeg.

## How it works

```
Telegram voice note (ogg/opus, 48 kHz)
        │
        │  media.get_file() → download_as_bytearray()
        ▼
   bytes in memory (capped at MAX_VOICE_BYTES = 5 MB)
        │
        │  POST /api/stt/<entity_id>
        │  body:   the raw audio
        │  header: X-Speech-Content: format=ogg; codec=opus; sample_rate=16000;
        │                            bit_rate=16; channel=1; language=it-IT
        ▼
   Home Assistant STT provider  →  {"result": "success", "text": "accendi lo studio"}
        │
        ▼
   echoed to the user, then fed to _dispatch_text() — the same parser typed text uses
```

The transcription is echoed back *before* it is executed. Speech recognition is
imperfect, and seeing «spegni la cucina» when you said "spegni la camera"
explains a surprising outcome instantly.

## Accepted inputs

Voice notes, audio files and video notes. The container and codec declared to
Home Assistant are derived from Telegram's `mime_type`:

| Input | Declared as |
|---|---|
| voice note, video note, anything else | `format=ogg`, `codec=opus` |
| forwarded WAV file | `format=wav`, `codec=pcm` |

## The sample rate question

This is the one genuinely surprising part of the pipeline, and the reason it
needs no transcoding.

Home Assistant validates the `X-Speech-Content` header against the capabilities
the provider advertises on `GET /api/stt/<entity_id>`, and rejects a mismatch
with a 400 **before the audio is ever decoded**. Providers typically declare
support for `sample_rate=16000` only.

Telegram voice notes are 48 kHz. Declaring 48000 fails validation; declaring
16000 passes it, and the Ogg container carries its own sample rate, which the
decoder honours. So the declared value satisfies the check while the container
drives the actual decoding.

The practical consequence: **no resampling, no ffmpeg, no temporary files.**
The bytes go from Telegram straight into the STT request.

## Configuration

Nothing is required. At startup, if `HA_STT_ENTITY` is unset, the bot picks the
first `stt.` entity Home Assistant exposes and logs it:

```
… | Speech-to-text: stt.google_ai_stt (lingua it-IT)
```

Set the variables to override:

```ini
HA_STT_ENTITY=stt.google_ai_stt
STT_LANGUAGE=it-IT
```

If there is no `stt.` entity at all, the bot logs a warning at startup and
answers voice messages with an explanation. Typed commands are unaffected.

To add an engine, install any Assist-compatible STT integration in Home
Assistant (Google Generative AI, Whisper via Wyoming, and others). Check what
you have with:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://ha:8123/api/states \
  | jq -r '.[].entity_id | select(startswith("stt."))'
```

## Limits

| Limit | Value | Why |
|---|---|---|
| Clip size | 5 MB (`MAX_VOICE_BYTES`), ~5 minutes of Opus | Transcription is billed by the provider; the cap is checked *before* downloading, so an accidental long recording costs nothing. |
| Memory | the whole clip is held in RAM | At 5 MB this is fine; it is the reason the cap exists at all. |

## Troubleshooting

**"🎙 Nessun motore speech-to-text configurato in Home Assistant."**
No `stt.` entity was found at startup. Install an STT integration, or set
`HA_STT_ENTITY` if you have one under a name the auto-detection missed. The bot
detects engines **once, at startup** — restart it after adding one.

**"🎙 Non sono riuscito a trascrivere il vocale." followed by an HTTP error**
The provider rejected the request. Ask it what it accepts:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://ha:8123/api/stt/stt.google_ai_stt | jq .
```

The response lists supported formats, codecs, sample rates, bit rates, channel
counts and languages. Compare it against the header in
`HomeAssistantClient.speech_to_text` — a `400` almost always means one field
does not appear in that list. `STT_LANGUAGE` must be one of the advertised
languages.

**"🎙 Non ho sentito nulla di comprensibile, riprova."**
The provider returned success with empty text: silence, background noise, or a
language the engine was not expecting. Not an error.

**The transcription is right but the bot answers "Non ho capito"**
The parser did not recognise the phrasing. The echoed text tells you exactly
what to work with — see [development.md](development.md#teaching-it-a-new-phrasing).

**"🎙 Vocale troppo lungo"**
Over 5 MB. Raise `MAX_VOICE_BYTES` in [`bot.py`](../bot.py) if you really need
longer clips, keeping in mind that the whole clip is buffered in memory and
billed by the provider.
