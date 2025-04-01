#!/usr/bin/env python3
import asyncio
import json
import sys

import websockets
import gbulb

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC, GObject

# Install gbulb so GLib (GStreamer) + asyncio share the same loop
gbulb.install()

################################################################################
# Configuration
################################################################################

SIGNALING_SERVER = "ws://localhost:8765"  # e.g. your server
CAMERA_URL = "rtsp://admin:Password@192.168.0.201:554/Streaming/channels/101"

pipeline = None
webrtcbin = None
ws = None

# We'll store our main asyncio event loop here to schedule from GStreamer callbacks
main_loop = None

################################################################################
# GStreamer Pipeline Construction
################################################################################

def on_pad_added(src, new_pad, depay):
    """Link dynamic pads from rtspsrc to depay."""
    sink_pad = depay.get_static_pad("sink")
    if not sink_pad.is_linked():
        new_pad.link(sink_pad)

def link_many(*elements):
    """Helper to link multiple GStreamer elements in series."""
    for i in range(len(elements) - 1):
        if not elements[i].link(elements[i+1]):
            print(f"Failed to link {elements[i].get_name()} -> {elements[i+1].get_name()}")
            return False
    return True

def link_webrtc_branch(pad, info, capsfilter, webrtcbin):
    """Probe to link rtph265pay -> capsfilter -> webrtcbin at runtime when CAPS is known."""
    event = info.get_event()
    if event.type == Gst.EventType.CAPS:
        cf_sink = capsfilter.get_static_pad("sink")
        pad.link(cf_sink)

        cf_src = capsfilter.get_static_pad("src")
        webrtc_sink = webrtcbin.get_request_pad("sink_%u")
        cf_src.link(webrtc_sink)

        print("Dynamically linked pay -> capsfilter -> webrtcbin!")
        return Gst.PadProbeReturn.REMOVE
    return Gst.PadProbeReturn.OK

def create_pipeline():
    """Assemble our pipeline: RTSP -> (record MP4) + (WebRTC)."""
    global pipeline, webrtcbin

    pipeline = Gst.Pipeline.new("rtsp-webrtc-pipeline")

    # RTSP Source
    src = Gst.ElementFactory.make("rtspsrc", "src")
    src.set_property("location", CAMERA_URL)
    src.set_property("latency", 300)

    # Depay & parse
    depay = Gst.ElementFactory.make("rtph265depay", "depay")
    h265parse = Gst.ElementFactory.make("h265parse", "h265parse")
    h265parse.set_property("config-interval", 1)
    h265parse.set_property("disable-passthrough", True)

    # Tee
    tee = Gst.ElementFactory.make("tee", "tee")

    # ---- Recording Branch ----
    queue_rec = Gst.ElementFactory.make("queue", "queue_record")
    rec_parse = Gst.ElementFactory.make("h265parse", "record_parse")
    rec_parse.set_property("config-interval", 1)

    mux = Gst.ElementFactory.make("mp4mux", "muxer")
    mux.set_property("faststart", True)
    mux.set_property("streamable", True)
    mux.set_property("fragment-duration", 1000)

    fsink = Gst.ElementFactory.make("filesink", "filesink")
    fsink.set_property("location", "recorded_stream.mp4")

    # ---- WebRTC Branch ----
    queue_webrtc = Gst.ElementFactory.make("queue", "queue_webrtc")
    pay = Gst.ElementFactory.make("rtph265pay", "pay")
    pay.set_property("pt", 96)
    pay.set_property("config-interval", 1)

    capsfilter = Gst.ElementFactory.make("capsfilter", "capsfilter")
    caps = Gst.Caps.from_string("application/x-rtp,media=video,clock-rate=90000,encoding-name=H265,payload=96")
    capsfilter.set_property("caps", caps)

    webrtc = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
    webrtc.set_property("stun-server", "stun://stun.l.google.com:19302")

    # Add elements to pipeline
    for elem in (
        src, depay, h265parse, tee,
        queue_rec, rec_parse, mux, fsink,
        queue_webrtc, pay, capsfilter, webrtc
    ):
        if not elem:
            raise RuntimeError("Could not create GStreamer element(s).")
        pipeline.add(elem)

    # Connect dynamic pad from rtspsrc
    src.connect("pad-added", on_pad_added, depay)

    # Link core pipeline
    if not link_many(depay, h265parse, tee):
        raise RuntimeError("Failed linking depay->h265parse->tee")

    # Recording branch
    if not link_many(tee, queue_rec, rec_parse, mux, fsink):
        raise RuntimeError("Failed linking recording branch")

    # WebRTC branch
    if not tee.link(queue_webrtc):
        raise RuntimeError("Failed linking tee->queue_webrtc")

    if not queue_webrtc.link(pay):
        raise RuntimeError("Failed linking queue_webrtc->pay")

    # Use dynamic pad probe to link pay->capsfilter->webrtcbin
    pay.get_static_pad("src").add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM,
        lambda pad, info: link_webrtc_branch(pad, info, capsfilter, webrtc)
    )

    webrtcbin = webrtc
    return pipeline

################################################################################
# WebRTC: Negotiation + ICE
################################################################################

def on_negotiation_needed(element):
    """Called when webrtcbin wants to create an offer."""
    print("on_negotiation_needed: creating offer.")
    promise = Gst.Promise.new_with_change_func(on_offer_created, element, None)
    element.emit("create-offer", None, promise)

def on_offer_created(promise, _, __):
    """
    Called after create-offer. 
    We must set the local description (offer) and schedule sending the offer.
    """
    global webrtcbin
    reply = promise.get_reply()
    offer = reply.get_value("offer")

    webrtcbin.emit("set-local-description", offer, None)
    print("Set local description (offer). Sending to remote...")

    # Because we're in a GStreamer thread, schedule the coroutine on the main_loop
    main_loop.call_soon_threadsafe(
        asyncio.ensure_future,
        send_sdp_offer(offer)
    )

def on_ice_candidate(element, mlineindex, candidate):
    """
    Called when webrtcbin has a new local ICE candidate.
    We pass it to remote via WebSockets.
    """
    print(f"Local ICE candidate: {candidate}")
    if ws is None:
        print("Warning: no WebSocket connected, can't send ICE yet.")
        return
    msg = json.dumps({
        "type": "ice",
        "sdpMLineIndex": mlineindex,
        "candidate": candidate
    })
    main_loop.call_soon_threadsafe(
        asyncio.ensure_future,
        ws.send(msg)
    )

################################################################################
# Signaling
################################################################################

async def send_sdp_offer(offer):
    """Send our SDP offer to the remote peer via WebSockets."""
    global ws
    if ws is None:
        print("Warning: no websocket connected, can't send offer.")
        return
    text = offer.sdp.as_text()
    msg = {"type": "offer", "sdp": text}
    await ws.send(json.dumps(msg))
    print("Sent SDP offer to remote.")

async def handle_signaling():
    """Connect to the signaling server, handle inbound messages."""
    global ws
    try:
        ws = await websockets.connect(SIGNALING_SERVER)
        print(f"Connected to {SIGNALING_SERVER}, waiting for remote answer...")

        async for message in ws:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "answer":
                sdp = Gst.SDPMessage.new()
                Gst.SDPMessage.parse_buffer(data["sdp"].encode(), sdp)
                answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)

                # Must do set-remote-description in the main GStreamer thread
                def apply_answer():
                    webrtcbin.emit("set-remote-description", answer, None)
                    print("Remote answer applied.")

                main_loop.call_soon_threadsafe(apply_answer)

            elif msg_type == "ice":
                candidate = data["candidate"]
                sdp_mline_index = data["sdpMLineIndex"]
                def add_remote_ice():
                    webrtcbin.emit("add-ice-candidate", sdp_mline_index, candidate)
                    print(f"Remote ICE candidate added: {candidate}")

                main_loop.call_soon_threadsafe(add_remote_ice)

    except Exception as e:
        print(f"Signaling connection error: {e}")
    finally:
        if ws:
            await ws.close()
            ws = None
            print("WebSocket closed.")

################################################################################
# Main
################################################################################

async def main():
    """Main coroutine: create pipeline, run signaling, watch pipeline bus, never return."""
    global pipeline, webrtcbin, main_loop

    pipeline = create_pipeline()

    # Connect webrtc signals
    webrtcbin.connect("on-negotiation-needed", on_negotiation_needed)
    webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    # Set pipeline to PLAYING
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("Failed to set pipeline PLAYING.")
    print("Pipeline started; recording to recorded_stream.mp4 + WebRTC streaming.")

    # Start signaling
    asyncio.ensure_future(handle_signaling())

    # Watch GStreamer bus for errors/EOS
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_bus_message(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"[GStreamer] ERROR: {err}, debug={debug}")
            loop.stop()
        elif msg.type == Gst.MessageType.EOS:
            print("[GStreamer] EOS received, stopping pipeline.")
            loop.stop()

    bus.connect("message", on_bus_message)

    # Keep running forever
    await asyncio.Future()

if __name__ == "__main__":
    Gst.init(None)
    # 'threads_init()' is deprecated as of PyGObject 3.11, so no need.
    # GObject.threads_init()

    # Because gbulb is installed, we can directly get an integrated event loop:
    loop = asyncio.get_event_loop()
    main_loop = loop  # store reference for call_soon_threadsafe usage

    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("KeyboardInterrupt: Stopping main loop...")
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
    finally:
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
        loop.close()
        print("Event loop closed; pipeline set to NULL.")
