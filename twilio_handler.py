from __future__ import annotations
from dotenv import load_dotenv
import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from agents import function_tool
from agents.realtime import (
    RealtimeAgent,
    RealtimePlaybackTracker,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
)

from services import get_current_time, get_weather, insert_into_mongodb
load_dotenv()




# If modifying these scopes, delete the file token.json.
SCOPES = ["https://www.googleapis.com/auth/documents"]
DOCUMENT_ID = "1wu4CmQDhSET79vl8vOqGbgZIOEsty0V452B2M47EvTs"  # Replace with your Google Doc's ID


def get_docs_service():
    """Initializes and returns the Google Docs API service."""
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("docs", "v1", credentials=creds)


@function_tool
def write_to_google_doc(text: str) -> str:
    """Writes the given text to a Google Doc."""
    try:
        service = get_docs_service()
        # Find the end of the document to append text
        document = service.documents().get(documentId=DOCUMENT_ID).execute()
        end_of_doc_index = document.get("body").get("content")[-1].get("endIndex") - 1

        requests = [
            {
                "insertText": {
                    "location": {"index": end_of_doc_index},
                    "text": text + "\n",
                }
            }
        ]
        service.documents().batchUpdate(
            documentId=DOCUMENT_ID, body={"requests": requests}
        ).execute()
        return "Google Doc में सफलतापूर्वक लिखा गया।"
    except Exception as e:
        return f"Google Doc में लिखने में विफल: {e}"


agent = RealtimeAgent(
    name="Twilio Assistant",
    instructions="आप एक सहायक सहायक हैं जो हर बातचीत की शुरुआत एक रचनात्मक ग्रीटिंग के साथ करते हैं। प्रतिक्रियाओं को संक्षिप्त और मैत्रीपूर्ण रखें क्योंकि यह एक फ़ोन वार्तालाप है। आपको हिंदी में जवाब देना है।",
    tools=[get_weather, get_current_time, write_to_google_doc, insert_into_mongodb],

)

class TwilioHandler:
    def __init__(self, twilio_websocket: WebSocket):
        self.twilio_websocket = twilio_websocket
        self._message_loop_task: asyncio.Task[None] | None = None
        self.session: RealtimeSession | None = None
        self.playback_tracker = RealtimePlaybackTracker()

        # Audio buffering configuration (matching CLI demo)
        self.CHUNK_LENGTH_S = 0.05  # 50ms chunks like CLI demo
        self.SAMPLE_RATE = 8000  # Twilio uses 8kHz for g711_ulaw
        self.BUFFER_SIZE_BYTES = int(
            self.SAMPLE_RATE * self.CHUNK_LENGTH_S
        )  # 50ms worth of audio

        self._stream_sid: str | None = None
        self._audio_buffer: bytearray = bytearray()
        self._last_buffer_send_time = time.time()

        # Mark event tracking for playback
        self._mark_counter = 0
        self._mark_data: dict[
            str, tuple[str, int, int]
        ] = {}  # mark_id -> (item_id, content_index, byte_count)

    async def start(self) -> None:
        """Start the session."""
        runner = RealtimeRunner(agent)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

        self.session = await runner.run(
            model_config={
                "api_key": api_key,
                "initial_model_settings": {
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {
                        "type": "semantic_vad",
                        "interrupt_response": True,
                        "create_response": True,
                    },
                },
                "playback_tracker": self.playback_tracker,
            }
        )

        await self.session.enter()

        await self.twilio_websocket.accept()
        print("Twilio WebSocket connection accepted")

        self._realtime_session_task = asyncio.create_task(self._realtime_session_loop())
        self._message_loop_task = asyncio.create_task(self._twilio_message_loop())
        self._buffer_flush_task = asyncio.create_task(self._buffer_flush_loop())

    async def wait_until_done(self) -> None:
        """Wait until the session is done."""
        assert self._message_loop_task is not None
        await self._message_loop_task

    async def _realtime_session_loop(self) -> None:
        """Listen for events from the realtime session."""
        assert self.session is not None
        try:
            async for event in self.session:
                await self._handle_realtime_event(event)
        except Exception as e:
            print(f"Error in realtime session loop: {e}")

    async def _twilio_message_loop(self) -> None:
        """Listen for messages from Twilio WebSocket and handle them."""
        try:
            while True:
                message_text = await self.twilio_websocket.receive_text()
                message = json.loads(message_text)
                await self._handle_twilio_message(message)
        except json.JSONDecodeError as e:
            print(f"Failed to parse Twilio message as JSON: {e}")
        except Exception as e:
            print(f"Error in Twilio message loop: {e}")

    async def _handle_realtime_event(self, event: RealtimeSessionEvent) -> None:
        """Handle events from the realtime session."""
        if event.type == "audio":
            base64_audio = base64.b64encode(event.audio.data).decode("utf-8")
            await self.twilio_websocket.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": base64_audio},
                    }
                )
            )

            # Send mark event for playback tracking
            self._mark_counter += 1
            mark_id = str(self._mark_counter)
            self._mark_data[mark_id] = (
                event.audio.item_id,
                event.audio.content_index,
                len(event.audio.data),
            )

            await self.twilio_websocket.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self._stream_sid,
                        "mark": {"name": mark_id},
                    }
                )
            )

        elif event.type == "audio_interrupted":
            print("Sending audio interrupted to Twilio")
            await self.twilio_websocket.send_text(
                json.dumps({"event": "clear", "streamSid": self._stream_sid})
            )
        elif event.type == "audio_end":
            print("Audio end")
        elif event.type == "raw_model_event":
            pass
        else:
            pass

    async def _handle_twilio_message(self, message: dict[str, Any]) -> None:
        """Handle incoming messages from Twilio Media Stream."""
        try:
            event = message.get("event")

            if event == "connected":
                print("Twilio media stream connected")
            elif event == "start":
                start_data = message.get("start", {})
                self._stream_sid = start_data.get("streamSid")
                print(f"Media stream started with SID: {self._stream_sid}")
            elif event == "media":
                await self._handle_media_event(message)
            elif event == "mark":
                await self._handle_mark_event(message)
            elif event == "stop":
                print("Media stream stopped")
        except Exception as e:
            print(f"Error handling Twilio message: {e}")

    async def _handle_media_event(self, message: dict[str, Any]) -> None:
        """Handle audio data from Twilio - buffer it before sending to OpenAI."""
        media = message.get("media", {})
        payload = media.get("payload", "")

        if payload:
            try:
                # Decode base64 audio from Twilio (µ-law format)
                ulaw_bytes = base64.b64decode(payload)

                # Add original µ-law to buffer for OpenAI (they expect µ-law)
                self._audio_buffer.extend(ulaw_bytes)

                # Send buffered audio if we have enough data
                if len(self._audio_buffer) >= self.BUFFER_SIZE_BYTES:
                    await self._flush_audio_buffer()

            except Exception as e:
                print(f"Error processing audio from Twilio: {e}")

    async def _handle_mark_event(self, message: dict[str, Any]) -> None:
        """Handle mark events from Twilio to update playback tracker."""
        try:
            mark_data = message.get("mark", {})
            mark_id = mark_data.get("name", "")

            # Look up stored data for this mark ID
            if mark_id in self._mark_data:
                item_id, item_content_index, byte_count = self._mark_data[mark_id]

                # Convert byte count back to bytes for playback tracker
                audio_bytes = b"\x00" * byte_count  # Placeholder bytes

                # Update playback tracker
                self.playback_tracker.on_play_bytes(item_id, item_content_index, audio_bytes)
                print(
                    f"Playback tracker updated: {item_id}, index {item_content_index}, {byte_count} bytes"
                )

                # Clean up the stored data
                del self._mark_data[mark_id]

        except Exception as e:
            print(f"Error handling mark event: {e}")

    async def _flush_audio_buffer(self) -> None:
        """Send buffered audio to OpenAI."""
        if not self._audio_buffer or not self.session:
            return

        try:
            # Send the buffered audio
            buffer_data = bytes(self._audio_buffer)
            await self.session.send_audio(buffer_data)

            # Clear the buffer
            self._audio_buffer.clear()
            self._last_buffer_send_time = time.time()

        except Exception as e:
            print(f"Error sending buffered audio to OpenAI: {e}")

    async def _buffer_flush_loop(self) -> None:
        """Periodically flush audio buffer to prevent stale data."""
        try:
            while True:
                await asyncio.sleep(self.CHUNK_LENGTH_S)  # Check every 50ms

                # If buffer has data and it's been too long since last send, flush it
                current_time = time.time()
                if (
                    self._audio_buffer
                    and current_time - self._last_buffer_send_time > self.CHUNK_LENGTH_S * 2
                ):
                    await self._flush_audio_buffer()

        except Exception as e:
            print(f"Error in buffer flush loop: {e}")