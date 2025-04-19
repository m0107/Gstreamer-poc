# file: main.py

import os
import logging
import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from discovery import discover_onvif, get_rtsp_uri
from pipeline import StreamPipeline

import gi

# ————— GStreamer introspection —————
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp

# Initialize GStreamer (needed before parse_launch or any use)
Gst.init(None)

# ————— Logging setup —————
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)
logger = logging.getLogger('main')

# ————— Load environment —————
load_dotenv()
STUN_SERVER = os.getenv('STUN_SERVER')

# ————— FastAPI setup —————
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Holds the running pipelines and their WebSocket objects
active_streams = {}  # stream_id → {'pipeline': StreamPipeline, 'ws': WebSocket}

# ————— REST endpoints —————

@app.get('/cameras')
def list_cameras():
    logger.debug("GET /cameras called")
    cams = discover_onvif()
    logger.debug(f"Discovered cameras: {cams}")
    return cams

@app.post('/streams')
def start_stream(req: dict):
    logger.info(f"POST /streams payload: {req}")
    service_url = req.get('service_url')
    if not service_url:
        logger.error("Missing 'service_url' in request")
        raise HTTPException(400, "'service_url' required")
    try:
        rtsp_uri = get_rtsp_uri(service_url)
        logger.info(f"Resolved RTSP URI: {rtsp_uri}")
    except Exception as e:
        logger.exception("ONVIF discovery failed")
        raise HTTPException(500, f"ONVIF error: {e}")
    sp = StreamPipeline(rtsp_uri, STUN_SERVER)
    sp.start()
    active_streams[sp.id] = {'pipeline': sp, 'ws': None}
    logger.info(f"Stream started with id: {sp.id}")
    return {'stream_id': sp.id}

@app.delete('/streams/{stream_id}')
def stop_stream(stream_id: str):
    logger.info(f"DELETE /streams/{stream_id}")
    info = active_streams.pop(stream_id, None)
    if not info:
        logger.warning(f"Stream {stream_id} not found")
        raise HTTPException(404, "Stream not found")
    info['pipeline'].stop()
    logger.info(f"Stream {stream_id} stopped")
    return {'stopped': stream_id}

# ————— WebSocket / signaling endpoint —————

@app.websocket('/ws/{stream_id}')
async def signaling(ws: WebSocket, stream_id: str):
    logger.info(f"WebSocket handshake for stream {stream_id}")
    await ws.accept()
    logger.info(f"WebSocket accepted for stream {stream_id}")

    info = active_streams.get(stream_id)
    if not info:
        logger.error(f"Stream {stream_id} not found on WS connect")
        await ws.close()
        return

    info['ws'] = ws
    pipeline = info['pipeline'].pipeline
    webrtc = pipeline.get_by_name('webrtc')
    loop = asyncio.get_running_loop()

    # — ICE candidate → browser
    def on_ice_candidate(element, mline, candidate):
        logger.debug(f"[{stream_id}] on-ice-candidate: mline={mline}, cand={candidate}")
        coro = ws.send_text(f"ICE:{mline}:{candidate}")
        asyncio.run_coroutine_threadsafe(coro, loop)

    # — Offer created → browser
    def on_offer_created(promise, _):
        logger.info(f"[{stream_id}] on-offer-created")
        reply = promise.get_reply()
        offer = reply.get_value("offer")  # GstWebRTCSessionDescription
        sdp = offer.sdp             # GstSdp.SDPMessage
        sdp_text = sdp.as_text()
        logger.info(f"[{stream_id}] SDP offer:\n{sdp_text}")
        # set our local description so ICE gathering starts
        webrtc.emit("set-local-description", offer, None)
        coro = ws.send_text(sdp_text)
        asyncio.run_coroutine_threadsafe(coro, loop)

    # — Negotiation needed
    def on_negotiation_needed(element):
        logger.info(f"[{stream_id}] on-negotiation-needed")
        promise = Gst.Promise.new_with_change_func(on_offer_created, None)
        element.emit("create-offer", None, promise)
        logger.info(f"[{stream_id}] create-offer emitted")

    # Hook up the signals
    webrtc.connect("on-ice-candidate", on_ice_candidate)
    webrtc.connect("on-negotiation-needed", on_negotiation_needed)

    try:
        while True:
            msg = await ws.receive_text()
            prefix = msg[:4]
            logger.info(f"[{stream_id}] WS recv ({prefix!r})…")
            if msg.startswith("v=0"):
                # SDP answer from browser
                _, sdp_msg = GstSdp.SDPMessage.new_from_text(msg)
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg)
                webrtc.emit("set-remote-description", answer, None)
                logger.debug(f"[{stream_id}] set-remote-description done")
            elif msg.startswith("ICE:"):
                _, mline, cand = msg.split(":", 2)
                logger.debug(f"[{stream_id}] browser ICE → mline={mline}")
                webrtc.emit("add-ice-candidate", int(mline), cand)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for stream {stream_id}")
        info['pipeline'].stop()
    except Exception as e:
        logger.exception(f"Error in signaling for stream {stream_id}")
        info['pipeline'].stop()
        await ws.close()
