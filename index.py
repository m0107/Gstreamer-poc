#!/usr/bin/env python3
"""
index.py - A GStreamer pipeline for recording an RTSP stream and relaying it via WebRTC.

This script:
  - Pulls an RTSP stream from CAMERA_URL.
  - Records it to an MP4 file.
  - Streams the video via WebRTC using webrtcbin.
  - Exchanges SDP and ICE candidates with a remote peer via a signaling server.

Make sure an RTSP publisher is running at CAMERA_URL.
"""

import asyncio
import json
import sys

import websockets
import gbulb

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
from gi.repository import Gst, GstWebRTC

# Install gbulb to integrate GLib (GStreamer) with asyncio.
gbulb.install()

################################################################################
# Configuration
################################################################################

# URL of the signaling server
SIGNALING_SERVER = "ws://localhost:8765"
# RTSP stream URL (make sure a publisher is running at this address)
CAMERA_URL = "rtsp://admin:Password@192.168.1.201:554/streaming/101"

# stream_url = "rtsp://admin:Password@192.168.1.201:554/streaming/101"

# Global variables for the pipeline, webrtcbin, and websocket.
pipeline = None
webrtcbin = None
ws = None

# We'll assign the main asyncio loop inside the main() function.
main_loop = None

################################################################################
# Helper Functions and Callbacks
################################################################################

def check_elem(name, elem):
    """Check if a GStreamer element was created successfully; log and raise error if not."""
    if not elem:
        raise RuntimeError(f"Could not create GStreamer element: {name}")
    print(f"[DEBUG] Created element: {name}")
    return elem

def on_pad_added(src, new_pad, depay):
    """Callback: Link dynamic pads from rtspsrc to the depayloader."""
    print("[DEBUG] on_pad_added: New pad added, attempting to link to depayloader")
    sink_pad = depay.get_static_pad("sink")
    if not sink_pad.is_linked():
        result = new_pad.link(sink_pad)
        print(f"[DEBUG] Linking result: {result}")

def link_many(*elements):
    """Link a series of GStreamer elements and log each linking step."""
    for i in range(len(elements) - 1):
        src_elem = elements[i]
        sink_elem = elements[i+1]
        if not src_elem.link(sink_elem):
            print(f"[ERROR] Failed to link {src_elem.get_name()} -> {sink_elem.get_name()}")
            return False
        print(f"[DEBUG] Successfully linked {src_elem.get_name()} -> {sink_elem.get_name()}")
    return True

def link_webrtc_branch(pad, info, capsfilter, webrtcbin):
    """
    When the payload element emits a CAPS event, dynamically link:
      rtph264pay -> capsfilter -> webrtcbin.
      (Changed from H.265 references to H.264)
    """
    event = info.get_event()
    if event.type == Gst.EventType.CAPS:
        print("[DEBUG] CAPS event detected in webrtc branch; linking elements.")
        cf_sink = capsfilter.get_static_pad("sink")
        pad.link(cf_sink)
        cf_src = capsfilter.get_static_pad("src")
        webrtc_sink = webrtcbin.get_request_pad("sink_%u")
        cf_src.link(webrtc_sink)
        print("[INFO] Dynamically linked pay -> capsfilter -> webrtcbin (H.264)!")
        return Gst.PadProbeReturn.REMOVE
    return Gst.PadProbeReturn.OK

def create_pipeline():
    """Assemble and link the GStreamer pipeline."""
    global pipeline, webrtcbin

    print("[INFO] Creating GStreamer pipeline...")
    pipeline = Gst.Pipeline.new("rtsp-webrtc-pipeline")

    # --- Source: RTSP stream ---
    src = check_elem("rtspsrc", Gst.ElementFactory.make("rtspsrc", "src"))
    src.set_property("location", CAMERA_URL)
    src.set_property("latency", 300)
    src.set_property("protocols", 4)  # Force TCP
    print(f"[INFO] rtspsrc configured with CAMERA_URL: {CAMERA_URL}")

    # --- Depayload and parse ---
    # Changed from rtph265depay -> rtph264depay, h265parse -> h264parse
    depay = check_elem("rtph264depay", Gst.ElementFactory.make("rtph264depay", "depay"))
    h264parse = check_elem("h264parse", Gst.ElementFactory.make("h264parse", "h264parse"))
    # config-interval is valid for h264parse as well
    h264parse.set_property("config-interval", 1)
    h264parse.set_property("disable-passthrough", True)

    # --- Tee: split the stream for recording and WebRTC ---
    tee = check_elem("tee", Gst.ElementFactory.make("tee", "tee"))

    # --- Recording Branch ---
    queue_rec = check_elem("queue_record", Gst.ElementFactory.make("queue", "queue_record"))
    # For the recording parse, changed from h265parse -> h264parse
    rec_parse = check_elem("record_parse", Gst.ElementFactory.make("h264parse", "record_parse"))
    rec_parse.set_property("config-interval", 1)
    mux = check_elem("mp4mux", Gst.ElementFactory.make("mp4mux", "muxer"))
    mux.set_property("faststart", True)
    mux.set_property("streamable", True)
    mux.set_property("fragment-duration", 1000)
    fsink = check_elem("filesink", Gst.ElementFactory.make("filesink", "filesink"))
    fsink.set_property("location", "recorded_stream.mp4")

    # --- WebRTC Branch ---
    queue_webrtc = check_elem("queue_webrtc", Gst.ElementFactory.make("queue", "queue_webrtc"))
    # Changed from rtph265pay -> rtph264pay
    pay = check_elem("rtph264pay", Gst.ElementFactory.make("rtph264pay", "pay"))
    pay.set_property("pt", 96)
    pay.set_property("config-interval", 1)
    capsfilter = check_elem("capsfilter", Gst.ElementFactory.make("capsfilter", "capsfilter"))
    # Changed encoding-name=H265 -> H264
    caps = Gst.Caps.from_string("application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96")
    capsfilter.set_property("caps", caps)
    webrtc = check_elem("webrtcbin", Gst.ElementFactory.make("webrtcbin", "webrtcbin"))
    webrtc.set_property("stun-server", "stun://stun.l.google.com:19302")

    # --- Add all elements to the pipeline ---
    for elem in (
        src, depay, h264parse, tee,
        queue_rec, rec_parse, mux, fsink,
        queue_webrtc, pay, capsfilter, webrtc
    ):
        pipeline.add(elem)
        print(f"[DEBUG] Added {elem.get_name()} to pipeline.")

    # Connect dynamic pad from rtspsrc to depayloader.
    src.connect("pad-added", on_pad_added, depay)

    # Link core elements: depay -> h264parse -> tee.
    if not link_many(depay, h264parse, tee):
        raise RuntimeError("Failed linking depay -> h264parse -> tee")

    # Link recording branch: tee -> queue_record -> record_parse -> mux -> filesink.
    if not link_many(tee, queue_rec, rec_parse, mux, fsink):
        raise RuntimeError("Failed linking recording branch")

    # Link WebRTC branch: tee -> queue_webrtc -> pay.
    if not tee.link(queue_webrtc):
        raise RuntimeError("Failed linking tee -> queue_webrtc")
    if not queue_webrtc.link(pay):
        raise RuntimeError("Failed linking queue_webrtc -> pay")

    # Dynamically link the WebRTC branch once CAPS are negotiated.
    pay.get_static_pad("src").add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM,
        lambda pad, info: link_webrtc_branch(pad, info, capsfilter, webrtc)
    )

    webrtcbin = webrtc
    print("[INFO] Pipeline creation successful.")
    return pipeline

def on_negotiation_needed(element):
    """Triggered when webrtcbin needs to create an SDP offer."""
    print("[DEBUG] on_negotiation_needed called; creating offer.")
    promise = Gst.Promise.new_with_change_func(on_offer_created, element, None)
    element.emit("create-offer", None, promise)

def on_offer_created(promise, _, __):
    """
    Called when the SDP offer is created.
    Sets the local description and schedules sending the offer via the signaling channel.
    """
    global webrtcbin
    reply = promise.get_reply()
    offer = reply.get_value("offer")
    webrtcbin.emit("set-local-description", offer, None)
    print("[DEBUG] Local description set; scheduling offer send.")
    main_loop.call_soon_threadsafe(asyncio.ensure_future, send_sdp_offer(offer))

def on_ice_candidate(element, mlineindex, candidate):
    """Called when a local ICE candidate is found. Sends it to the remote peer."""
    print(f"[DEBUG] Local ICE candidate: {candidate}")
    if ws:
        msg = json.dumps({
            "type": "ice",
            "sdpMLineIndex": mlineindex,
            "candidate": candidate
        })
        main_loop.call_soon_threadsafe(asyncio.ensure_future, ws.send(msg))
        print("[DEBUG] Sent ICE candidate to remote.")

async def send_sdp_offer(offer):
    """Send the SDP offer to the remote peer via the signaling server."""
    global ws
    if ws:
        text = offer.sdp.as_text()
        msg = {"type": "offer", "sdp": text}
        await ws.send(json.dumps(msg))
        print("[DEBUG] SDP offer sent to remote.")

async def handle_signaling():
    """Connect to the signaling server and handle incoming signaling messages."""
    global ws
    try:
        ws = await websockets.connect(SIGNALING_SERVER)
        print(f"[INFO] Connected to signaling server at {SIGNALING_SERVER}")
        async for message in ws:
            print("[DEBUG] Received signaling message.")
            data = json.loads(message)
            msg_type = data.get("type")
            if msg_type == "answer":
                sdp = Gst.SDPMessage.new()
                Gst.SDPMessage.parse_buffer(data["sdp"].encode(), sdp)
                answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
                def apply_answer():
                    webrtcbin.emit("set-remote-description", answer, None)
                    print("[INFO] Remote SDP answer applied.")
                main_loop.call_soon_threadsafe(apply_answer)
            elif msg_type == "ice":
                candidate = data["candidate"]
                sdp_mline_index = data["sdpMLineIndex"]
                def add_remote_ice():
                    webrtcbin.emit("add-ice-candidate", sdp_mline_index, candidate)
                    print(f"[INFO] Remote ICE candidate added: {candidate}")
                main_loop.call_soon_threadsafe(add_remote_ice)
    except Exception as e:
        print(f"[ERROR] Signaling connection error: {e}")
    finally:
        if ws:
            await ws.close()
            print("[INFO] WebSocket signaling connection closed.")

async def main():
    """Main coroutine: creates the pipeline, starts signaling, and watches the pipeline bus."""
    global pipeline, webrtcbin, main_loop

    print("[INFO] Starting main function...")
    # Obtain the running event loop for call_soon_threadsafe
    main_loop = asyncio.get_running_loop()

    # Create and configure the GStreamer pipeline.
    pipeline = create_pipeline()

    # Connect WebRTC signals for SDP negotiation and ICE handling.
    webrtcbin.connect("on-negotiation-needed", on_negotiation_needed)
    webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    # Attempt to set the pipeline to PLAYING state.
    ret = pipeline.set_state(Gst.State.PLAYING)
    print("[DEBUG] Setting pipeline to PLAYING state...")
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("Failed to set pipeline PLAYING. Check that the RTSP source is active!")
    print("[INFO] Pipeline is now PLAYING; recording to recorded_stream.mp4 + WebRTC streaming.")

    # Start signaling asynchronously.
    asyncio.ensure_future(handle_signaling())

    # Watch the GStreamer bus for errors or EOS.
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    def on_bus_message(bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"[ERROR] [GStreamer] {err}, debug: {debug}")
            asyncio.get_running_loop().stop()
        elif msg.type == Gst.MessageType.EOS:
            print("[INFO] [GStreamer] EOS received; stopping pipeline.")
            asyncio.get_running_loop().stop()
    bus.connect("message", on_bus_message)

    # Keep the main coroutine alive indefinitely.
    await asyncio.Future()

if __name__ == "__main__":
    # Initialize GStreamer and install gbulb.
    Gst.init(None)
    gbulb.install()
    print("[INFO] GStreamer initialized.")

    try:
        # Start the main coroutine using asyncio.run().
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[INFO] KeyboardInterrupt: Shutting down...")
    finally:
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
            print("[INFO] Pipeline set to NULL.")
        print("[INFO] Event loop closed; shutdown complete.")