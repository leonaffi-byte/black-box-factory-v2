"""Multi-provider STT (speech-to-text) and TTS (text-to-speech)."""

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

import httpx
import edge_tts

from . import config, state

log = logging.getLogger(__name__)

# --- Audio conversion ---

def ogg_to_wav(ogg_path: str) -> str:
    """Convert Telegram .ogg voice to .wav for Whisper. Returns wav path."""
    wav_path = ogg_path.rsplit(".", 1)[0] + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path],
        capture_output=True, check=True,
    )
    return wav_path


# --- STT: Speech-to-Text ---

async def transcribe_groq(audio_path: str) -> str | None:
    """Transcribe audio using Groq Whisper API. Returns text or None on failure."""
    if not config.GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            with open(audio_path, "rb") as f:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"model": "whisper-large-v3", "language": "he"},
                )
            if resp.status_code == 200:
                return resp.json().get("text", "").strip()
            log.warning("Groq STT failed: %d %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("Groq STT error: %s", e)
    return None


async def transcribe_openai(audio_path: str) -> str | None:
    """Transcribe audio using OpenAI Whisper API. Returns text or None on failure."""
    if not config.OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            with open(audio_path, "rb") as f:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"model": "whisper-1"},
                )
            if resp.status_code == 200:
                return resp.json().get("text", "").strip()
            log.warning("OpenAI STT failed: %d %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("OpenAI STT error: %s", e)
    return None


async def transcribe(audio_path: str) -> str:
    """Transcribe audio using configured provider(s). Returns transcribed text."""
    settings = state.load_settings()
    provider = settings.get("stt_provider", "auto")

    if provider == "groq":
        result = await transcribe_groq(audio_path)
        if result:
            return result
        return "[Transcription failed - Groq unavailable]"

    if provider == "openai":
        result = await transcribe_openai(audio_path)
        if result:
            return result
        return "[Transcription failed - OpenAI unavailable]"

    # Auto mode: try Groq first, fallback to OpenAI
    result = await transcribe_groq(audio_path)
    if result:
        return result
    log.info("Groq STT failed, falling back to OpenAI")
    result = await transcribe_openai(audio_path)
    if result:
        return result
    return "[Transcription failed - all providers unavailable]"


# --- TTS: Text-to-Speech ---

async def tts_edge(text: str, voice: str | None = None) -> str | None:
    """Generate speech using edge-tts (Microsoft, free). Returns ogg path or None."""
    settings = state.load_settings()
    voice = voice or settings.get("tts_voice", "he-IL-HilaNeural")

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            mp3_path = tmp.name

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(mp3_path)

        # Convert mp3 to ogg for Telegram voice message
        ogg_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
        subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", ogg_path],
            capture_output=True, check=True,
        )
        Path(mp3_path).unlink(missing_ok=True)
        return ogg_path
    except Exception as e:
        log.warning("edge-tts error: %s", e)
        return None


async def tts_openai(text: str) -> str | None:
    """Generate speech using OpenAI TTS. Returns ogg path or None."""
    if not config.OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": "tts-1", "input": text, "voice": "nova", "response_format": "opus"},
            )
            if resp.status_code == 200:
                with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                    tmp.write(resp.content)
                    return tmp.name
            log.warning("OpenAI TTS failed: %d", resp.status_code)
    except Exception as e:
        log.warning("OpenAI TTS error: %s", e)
    return None


async def text_to_speech(text: str) -> str | None:
    """Convert text to speech using configured provider. Returns ogg path or None."""
    settings = state.load_settings()
    provider = settings.get("tts_provider", "edge")

    if provider == "openai":
        return await tts_openai(text)

    # Default: edge-tts (free)
    result = await tts_edge(text)
    if result:
        return result
    # Fallback to OpenAI if edge-tts fails
    return await tts_openai(text)
