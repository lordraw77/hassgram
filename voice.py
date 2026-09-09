"""Voice notes: transcribe a clip and run it as if it had been typed.

Voice is the one entry point that needs a service the rest of the bot does not
-- Home Assistant's speech-to-text -- and it is the one with its own refusals
(no engine configured, clip too long, nothing recognised). Keeping it here means
the rest of :mod:`bot` never has to think about audio.

The handler takes the bot as its first argument rather than being a method on
it: everything it needs from :class:`~bot.HassBot` is then visible in the
signature, and the module stays importable without one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import i18n
from constants import MAX_VOICE_BYTES
from ha_client import HomeAssistantError
from i18n import t
from views import esc, ha_error_text

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for type checkers only
    from bot import HassBot

log = logging.getLogger("hassgram.voice")


async def on_voice(bot: "HassBot", update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a voice message and execute it as if it had been typed.

    Accepts voice notes, audio files and video notes. The clip is downloaded from
    Telegram, sent to Home Assistant's speech-to-text engine, echoed back to the
    user, and then run through the same :meth:`bot.HassBot._dispatch_text` as typed text.

    Echoing the transcription before acting is a deliberate design choice: speech
    recognition is imperfect, and seeing "spegni la cucina" when you said "spegni
    la camera" explains a surprising outcome instantly. The typing action shown
    while the clip uploads is the only feedback available during what can be a
    couple of seconds of network work.

    The language handed to the engine is the chat's current one, since audio
    carries no language of its own: a chat that has been speaking English
    gets ``en-US``, one speaking Italian gets ``it-IT`` (see
    :attr:`bot.HassBot.stt_languages`). To dictate in the other language, write one
    message in it -- or run ``/language`` -- before recording.

    Three refusals, each with its own explanation: no STT engine configured, a clip
    larger than :data:`MAX_VOICE_BYTES`, or a transcription that came back empty.
    None of them is an error -- text commands remain available throughout.

    Note:
        Transcription is charged to the configured provider, and the size cap is
        the only thing standing between an accidental long recording and a large
        bill, so it is enforced before the clip is downloaded.
    """
    if not await bot.guard(update):
        return
    lang = bot.lang_of(update)
    message = update.effective_message
    media = message.voice or message.audio or message.video_note
    if media is None:
        return
    if not bot.stt_entity:
        await bot.reply(update, t(lang, "stt_missing"), lang)
        return
    if getattr(media, "file_size", 0) and media.file_size > MAX_VOICE_BYTES:
        await bot.reply(update, t(lang, "voice_too_long", mb=MAX_VOICE_BYTES // (1024 * 1024)), lang)
        return

    await message.chat.send_action(ChatAction.TYPING)
    stt_language = bot.stt_languages.get(lang, bot.stt_languages[i18n.DEFAULT_LANG])
    try:
        audio_file = await media.get_file()
        audio = bytes(await audio_file.download_as_bytearray())
        fmt, codec = audio_format(getattr(media, "mime_type", None))
        text = await bot.ha.speech_to_text(
            audio, bot.stt_entity, language=stt_language, audio_format=fmt, codec=codec
        )
    except HomeAssistantError as exc:
        log.warning("STT failed (%s): %s", stt_language, exc)
        await bot.reply(update, t(lang, "stt_failed", error=esc(ha_error_text(lang, exc))), lang)
        return

    if not text:
        await bot.reply(update, t(lang, "stt_empty"), lang)
        return

    log.info("Transcribed (%s): %r", stt_language, text)
    await bot.reply(update, t(lang, "transcribed", text=esc(text)), lang)
    await bot._dispatch_text(update, text, spoken=True)


def audio_format(mime_type: str | None) -> tuple[str, str]:
    """Derive the container and codec to declare for a Telegram clip.

    Args:
        mime_type: The ``mime_type`` Telegram reports, if any.

    Returns:
        A ``(format, codec)`` pair: ``("wav", "pcm")`` for a forwarded WAV file,
        ``("ogg", "opus")`` otherwise. Ogg/Opus is what voice notes and video notes
        always are, and it is what Home Assistant's STT providers accept natively,
        which is why Hassgram needs no ffmpeg and no transcoding step.
    """
    if mime_type and "wav" in mime_type:
        return "wav", "pcm"
    return "ogg", "opus"
