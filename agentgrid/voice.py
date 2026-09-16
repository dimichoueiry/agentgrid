"""Small voice-command trial; keys and generated speech stay in server memory."""
import base64
import binascii
import secrets
import json
import os
import re
import threading
import urllib.error
import urllib.request


class Voice:
    def __init__(self):
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.lock = threading.Lock()
        self.cache = {}

    def api_error(self, error, operation):
        message = ""
        code = ""
        try:
            payload = json.loads(error.read(16384))
            detail = payload.get("error", {})
            if isinstance(detail, dict):
                message = str(detail.get("message") or "")
                code = str(detail.get("code") or "")
        except (ValueError, OSError, AttributeError):
            pass
        if self.key:
            message = message.replace(self.key, "[redacted]")
        message = re.sub(r"sk-[A-Za-z0-9_\-]+", "[redacted]", message)
        message = " ".join(message.split())[:900]
        code = re.sub(r"[^a-zA-Z0-9_\-]", "", code)[:80]
        if not message:
            message = {401: "OpenAI rejected the API key.",
                       403: "Access was denied. Check this key’s permissions and its project’s model access.",
                       429: "Check your OpenAI API billing and rate limits."}.get(error.code, "The service did not provide a readable error message.")
        return f"{operation} (HTTP {error.code}" + (f", {code}" if code else "") + f"): {message}"

    def configure(self, key):
        if not isinstance(key, str) or (key and (not key.startswith("sk-") or len(key) > 1000 or any(c.isspace() for c in key))):
            raise ValueError("Enter a valid OpenAI API key.")
        self.key = key
        self.cache.clear()

    def command(self, command, snapshot):
        raw = " ".join(str(command).strip().split())
        words = re.sub(r"[^a-z0-9 ._/-]", "", raw.lower()).strip()
        if (words in ("help", "list commands", "show commands", "what can i say", "what commands are available")
                or ("help" in words and ("command" in words or "say" in words))
                or ("list" in words and "command" in words)):
            return {"reply": "You can ask what needs your attention, start a new agent, send a message to an agent, read its latest reply, or switch between Cedar and Marin."}
        voice_match = re.search(r"\b(?:use|switch(?: the)? voice to|speak with)\s+(?:the\s+)?(cedar|marin)\b", words)
        if voice_match:
            voice = voice_match.group(1)
            return {"voice": voice, "reply": f"Done. I’m using {voice.title()} for spoken updates. Your coding models stay the same."}
        if ("status" in words or "attention" in words or "happening" in words
                or re.search(r"what(?:'| wi)?s everyone (?:doing|working on)", words)):
            sessions = snapshot.get("sessions", [])
            working = sum(s.get("status") == "working" for s in sessions)
            waiting = sum(s.get("status") in ("blocked", "failed") for s in sessions)
            return {"reply": f"You have {working} working sessions and {waiting} sessions needing your attention."}
        start = re.search(r"\b(?:start|create|launch|open|spin up)\b.*\b(?:new\s+)?(?:agent|session)\b(?P<rest>.*)$", words)
        if start:
            rest = start.group("rest").strip(" .")
            engine = "codex" if re.search(r"\bcodex\b", words) else "claude"
            model_match = re.search(r"\b(?:using|use|with)\s+([a-z0-9][a-z0-9._/-]*)", rest)
            model = model_match.group(1) if model_match and model_match.group(1) not in ("this", "the", "a", "claude", "codex", "agent", "session") else ""
            task_match = re.search(r"\b(?:to|for)\s+(.+?)(?=\s+\bin\s+|$)", rest)
            task = task_match.group(1).strip(" .") if task_match else ""
            projects = snapshot.get("projects", [])
            project = None
            for candidate in projects:
                name = str(candidate.get("name") or candidate.get("label") or "").lower()
                if name and re.search(r"\b" + re.escape(name) + r"\b", rest):
                    project = candidate
                    break
            if not task:
                raise ValueError("Tell me what the new agent should do, for example: ‘start a Codex agent to add search in AgentGrid’." )
            if not project:
                raise ValueError("I have the task, but not the project. Say ‘in AgentGrid’ or choose a project in the board first.")
            return {"action": "spawn", "engine": engine, "model": model, "cwd": project.get("path"), "prompt": task,
                    "reply": f"Ready to start a {engine.title()} agent in {project.get('name') or project.get('label')}."}
        follow = re.search(r"\b(?:tell|ask|send)\b\s+(?:the\s+)?(?P<target>.+?)\s+(?:to|that)\s+(?P<message>.+)$", words)
        if follow:
            target = follow.group("target").strip()
            sessions = snapshot.get("sessions", [])
            matches = [s for s in sessions if target in str(s.get("title") or "").lower() or target in str(s.get("sessionId") or "").lower()]
            if len(matches) != 1:
                raise ValueError("I need one specific session name before I send that message.")
            session = matches[0]
            return {"action": "message", "sessionId": session.get("sessionId"), "message": follow.group("message").strip(" ."),
                    "reply": f"I’ll send that to {session.get('title') or 'the selected session'}."}
        raise ValueError('I didn’t recognize that. Try “What needs my attention?”, “start a new agent to…”, “tell [session] to…”, “Use Cedar”, or “Help”.')

    def speech(self, text, voice):
        if voice not in ("marin", "cedar") or not isinstance(text, str) or not 0 < len(text) <= 1500:
            raise ValueError("Invalid speech request.")
        if not self.key:
            raise ValueError("Add your OpenAI key below to hear replies.")
        if not self.lock.acquire(blocking=False):
            raise ValueError("Speech is already being generated. Try again shortly.")
        try:
            cache_key = (voice, text)
            if cache_key not in self.cache:
                payload = json.dumps({"model": "gpt-4o-mini-tts", "voice": voice, "input": text,
                    "instructions": "Speak naturally and concisely, like a helpful colleague.", "response_format": "mp3"}).encode()
                request = urllib.request.Request("https://api.openai.com/v1/audio/speech", data=payload,
                    headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=45) as response:
                    data = response.read()
                if len(self.cache) >= 10:
                    self.cache.clear()
                self.cache[cache_key] = data
            return self.cache[cache_key]
        except urllib.error.HTTPError as error:
            raise ValueError(self.api_error(error, "Mini TTS")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError("Could not reach OpenAI. Check your connection and try again.") from None
        finally:
            self.lock.release()

    def transcribe(self, encoded, mime):
        if not self.key:
            raise ValueError("Add your OpenAI key to use the microphone.")
        formats = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/wav": "wav"}
        mime = str(mime).split(";")[0]
        if mime not in formats or not isinstance(encoded, str) or len(encoded) > 8_000_000:
            raise ValueError("Unsupported or oversized audio recording.")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid audio recording.") from None
        if not audio:
            raise ValueError("No audio recorded. Please try again.")
        boundary = secrets.token_hex(24)
        chunks = []
        for name, value in {"model": "gpt-transcribe", "prompt": "Commands for Agent Grid. Names: Agent Grid, Codex, Claude, Cedar, Marin."}.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="command.{formats[mime]}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + audio + f'\r\n--{boundary}--\r\n'.encode())
        request = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", data=b"".join(chunks), headers={"Authorization": "Bearer " + self.key, "Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
            text = result.get("text", "")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("No speech recognized. Please try again.")
            return {"text": text.strip()}
        except urllib.error.HTTPError as error:
            raise ValueError(self.api_error(error, "GPT-Transcribe")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError("Could not reach OpenAI for transcription.") from None
