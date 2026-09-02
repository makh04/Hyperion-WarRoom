"""Manual smoke-test client: stands in for the Playwright Meet bot.

Connects your microphone + speakers to the SentinelVoice backend exactly the way the real
browser bot will, so you can sanity-check the whole pipeline (backend <-> AssemblyAI Voice
Agent, running on Groq) without needing Google Meet at all.

Usage:
    pip install sounddevice numpy requests websockets
    python scripts/mic_test_client.py --name "Local test"

Then say "Agent, check checkout-db CPU load" into your mic once connected.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import queue

import requests
import sounddevice as sd
import websockets

SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK_MS = 50
CHUNK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000


async def run(backend_http: str, backend_ws: str, name: str) -> None:
    resp = requests.post(f"{backend_http}/incidents", json={"name": name}, timeout=10)
    resp.raise_for_status()
    incident_id = resp.json()["incident_id"]
    print(f"Created incident {incident_id}")

    mic_queue: "queue.Queue[bytes]" = queue.Queue()

    def mic_callback(indata, frames, time_info, status):
        mic_queue.put(bytes(indata))

    out_stream = sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
    out_stream.start()

    uri = f"{backend_ws}/ws/bot/{incident_id}"
    async with websockets.connect(uri) as ws:
        print("Connected to bridge. Speak now - try: 'Agent, check checkout-db CPU load'")

        async def send_audio() -> None:
            loop = asyncio.get_event_loop()
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=CHUNK_FRAMES, callback=mic_callback,
            ):
                while True:
                    chunk = await loop.run_in_executor(None, mic_queue.get)
                    await ws.send(json.dumps({
                        "type": "audio.chunk",
                        "audio": base64.b64encode(chunk).decode(),
                    }))

        async def receive_audio() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "reply.audio":
                    out_stream.write(base64.b64decode(msg["data"]))

        await asyncio.gather(send_audio(), receive_audio())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://localhost:8000")
    parser.add_argument("--backend-ws", default="ws://localhost:8000")
    parser.add_argument("--name", default="Manual smoke test")
    args = parser.parse_args()
    asyncio.run(run(args.backend, args.backend_ws, args.name))
