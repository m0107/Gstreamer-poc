# main.py

import os
import logging
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from discovery import discover_onvif, get_rtsp_uri
from pipeline import StreamPipeline

load_dotenv()
STUN = os.getenv("STUN_SERVER")

app = FastAPI()
logging.basicConfig(level=logging.INFO)

# Allow browser clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

active_streams = {}  # stream_id → { pipeline: StreamPipeline, ws: WebSocket }

@app.get("/cameras")
def list_cameras():
    return discover_onvif()

@app.post("/streams")
def start_stream(req: dict):
    srv = req.get("service_url")
    if not srv:
        raise HTTPException(400, "'service_url' required")
    try:
        rtsp = get_rtsp_uri(srv)
    except Exception as e:
        raise HTTPException(500, f"ONVIF error: {e}")
    sp = StreamPipeline(rtsp, STUN)
    sp.start()
    active_streams[sp.id] = {"pipeline": sp, "ws": None}
    return {"stream_id": sp.id}

@app.delete("/streams/{stream_id}")
def stop_stream(stream_id: str):
    info = active_streams.pop(stream_id, None)
    if not info:
        raise HTTPException(404, "Stream not found")
    info["pipeline"].stop()
    return {"stopped": stream_id}

@app.websocket("/ws/{stream_id}")
async def signaling(ws: WebSocket, stream_id: str):
    await ws.accept()
    info = active_streams.get(stream_id)
    if not info:
        await ws.close(1000)
        return
    info["ws"] = ws
    pipeline = info["pipeline"].pipeline
    webrtc = pipeline.get_by_name("webrtc")
    
    # WebRTC callbacks
    def on_neg(elem): elem.emit("create-offer")
    webrtc.connect("on-negotiation-needed", on_neg)
    @webrtc.connect
    def on_offer_created(promise, _):
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        webrtc.emit("set-local-description", offer, None)
        ws.send_text(offer.sdp.as_text())
    @webrtc.connect
    def on_ice_candidate(elem, mline, cand):
        ws.send_text(f"ICE:{mline}:{cand}")

    try:
        while True:
            msg = await ws.receive_text()
            if msg.startswith("v=0"):  # SDP
                from gi.repository import GstSdp, GstWebRTC
                _, sdp = GstSdp.SDPMessage.new_from_text(msg)
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdp)
                webrtc.emit("set-remote-description", answer, None)
            elif msg.startswith("ICE:"):
                _, mline, cand = msg.split(":", 2)
                webrtc.emit("add-ice-candidate", int(mline), cand)
    except WebSocketDisconnect:
        info["pipeline"].stop()