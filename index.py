#!/usr/bin/env python3

import asyncio
import json
import websockets
import gbulb
import gi
import threading
import subprocess
import os
import uuid  # Used for naming fakesinks uniquely

# GStreamer initialization
gi.require_version("Gst", "1.0")
gi.require_version("GstSdp", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstRtsp", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC, GstRtsp

gbulb.install()

# Settings
SIGNALING_SERVER = "ws://localhost:8765"

# Central Data Structure
cameras = {}  # {camera_id: {url: str, pipeline: Gst.Pipeline, webrtc: Gst.Element}}

# Globals
ws = None
main_loop = None
start_time = None  # For forced timestamping (if needed)

def add_camera(camera_id, camera_url):
    global cameras
    pipeline, webrtcbin = create_pipeline(camera_id, camera_url)
    cameras[camera_id] = {"url": camera_url, "pipeline": pipeline, "webrtc": webrtcbin}

def remove_camera(camera_id):
    global cameras
    pipeline = cameras[camera_id]["pipeline"]
    pipeline.set_state(Gst.State.NULL)
    del cameras[camera_id]


#############################################
# Debug Probe for Logging at a Pad
#############################################

def debug_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        # if buf:
        # print(f"[DEBUG PROBE] Buffer reached {pad.get_name()} (size: {buf.get_size()} bytes)")
        return Gst.PadProbeReturn.OK
    return Gst.PadProbeReturn.OK

#############################################
# RTP Probe Function (for Logging Only)
#############################################

def rtp_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        # if buf:
        # print(f"[RTP PROBE] Buffer received on pad {pad.get_name()} (size: {buf.get_size()} bytes)")
        return Gst.PadProbeReturn.OK
    return Gst.PadProbeReturn.OK

#############################################
# Keyframe Probe Function for Renegotiation #
#############################################

def keyframe_probe(pad, info, camera_id):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        if buf and not (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT):
            print(f"[DEBUG] Keyframe detected for camera {camera_id}, triggering renegotiation.")
            trigger_renegotiation_if_needed(camera_id)  # Pass camera_id
        return Gst.PadProbeReturn.OK
    return Gst.PadProbeReturn.OK

#############################################
# Renegotiation Trigger Function #
#############################################

def trigger_renegotiation_if_needed(camera_id):  # Added camera_id
    global cameras, ws, main_loop

    if camera_id not in cameras or "webrtc" not in cameras[camera_id]:
        print(f"[DEBUG] webrtcbin is not set for camera {camera_id}; skipping renegotiation.")
        return

    webrtcbin = cameras[camera_id]["webrtc"]

    print(f"[DEBUG] Triggering renegotiation for camera {camera_id} now.")
    promise = Gst.Promise.new_with_change_func(on_offer_created, webrtcbin, camera_id)
    webrtcbin.emit("create-offer", None, promise)

#############################################
# Utility and Logging Probes #
#############################################

def force_timestamp_probe(pad, info):
    global start_time
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        if not buf:
            return Gst.PadProbeReturn.OK
        clock = Gst.SystemClock.obtain()
        current_time = clock.get_time()
        if start_time is None:
            start_time = current_time
        new_ts = current_time - start_time
        buf.pts = new_ts
        buf.dts = new_ts
        return Gst.PadProbeReturn.OK
    return Gst.PadProbeReturn.OK

def log_timestamp_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        # Uncomment below to log detailed timestamps:
        # print(f"[TIMESTAMP LOG] PTS: {buf.pts}, DTS: {buf.dts}, Size: {buf.get_size()} bytes")
        return Gst.PadProbeReturn.OK
    return Gst.PadProbeReturn.OK

def log_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        # Uncomment below for additional buffer logging
        # print(f"[LOG PROBE] Buffer at {pad.get_name()}; size: {buf.get_size()} bytes, PTS: {buf.pts}")
        return Gst.PadProbeReturn.OK
    return Gst.PadProbeReturn.OK

def has_audio_stream(rtsp_url):
    try:
        output = subprocess.check_output(
            f"ffprobe -loglevel error -show_streams {rtsp_url}", shell=True
        ).decode()
        return "codec_type=audio" in output
    except Exception as e:
        print(f"[WARN] Audio check failed: {e}")
        return False

def check_elem(name, elem):
    if not elem:
        raise RuntimeError(f"[FATAL] Failed to create GStreamer element: {name}")
    print(f"[DEBUG] Created element: {name}")
    return elem

def on_pad_added(src, new_pad, data):
    """
    Link the dynamic pad from the RTSP source:
    - If the pad’s caps is "application/x-rtp", link it to the target element.
    - Otherwise, link it to a fakesink.
    """
    camera_id = src.get_parent().name.split('-')[1] # Extract camera_id from pipeline name
    target_elem = data[0]  # Access tuple
    pipeline = data[1]
    print(f"[DEBUG] on_pad_added: Detected new pad {new_pad.get_name()} from {src.get_name()} for camera {camera_id}")

    caps = new_pad.get_current_caps() or new_pad.query_caps(None)
    structure = caps.get_structure(0)
    pad_type = structure.get_name()

    if pad_type != "application/x-rtp":
        print(f"[DEBUG] Non-RTP pad detected ({pad_type}); linking {new_pad.get_name()} to a fakesink.")
        fakesink = Gst.ElementFactory.make("fakesink", f"fakesink_{uuid.uuid4().hex}")
        if not fakesink:
            print("[ERROR] Could not create fakesink for non-RTP pad.")
            return

        pipeline.add(fakesink)
        fakesink.sync_state_with_parent()
        sink_pad = fakesink.get_static_pad("sink")
        ret = new_pad.link(sink_pad)

        if ret == Gst.PadLinkReturn.OK:
            print(f"[INFO] Successfully linked non-RTP pad {new_pad.get_name()} to fakesink.")
        else:
            print(f"[ERROR] Failed to link non-RTP pad {new_pad.get_name()} (error code: {ret}).")
        return

    # For RTP pads, link to the target element.
    sink_pad = target_elem.get_static_pad("sink")

    if not sink_pad:
        print("[ERROR] Target element does not have a sink pad.")
        return

    if sink_pad.is_linked():
        print("[WARNING] Target sink pad already linked; ignoring new pad.")
        return

    ret = new_pad.link(sink_pad)

    if ret == Gst.PadLinkReturn.OK:
        print("[INFO] Successfully linked dynamic RTP pad to identity element.")
    else:
        print(f"[ERROR] Failed to link dynamic pad {new_pad.get_name()} (error code: {ret}).")

def link_many(*elements):
    for i in range(len(elements) - 1):
        if not elements[i].link(elements[i+1]):
            print(f"[ERROR] Failed to link {elements[i].get_name()} -> {elements[i+1].get_name()}")
            return False
        print(f"[DEBUG] Linked {elements[i].get_name()} -> {elements[i+1].get_name()}")
    return True

#############################################
# Pipeline Creation
#############################################

def create_pipeline(camera_id, camera_url):
    print(f"[INFO] Creating pipeline for camera {camera_id}...")

    pipeline = Gst.Pipeline.new(f"camera-{camera_id}-pipeline")
    audio_present = has_audio_stream(camera_url)

    print(f"[INFO] Audio present in stream for camera {camera_id}: {audio_present}")

    # --- Common Video Branch ---
    src = check_elem("rtspsrc", Gst.ElementFactory.make("rtspsrc", f"src-{camera_id}"))
    src.set_property("location", camera_url)
    src.set_property("latency", 300)
    src.set_property("protocols", "udp")  # Force TCP for stability

    identity_timestamp = check_elem("identity", Gst.ElementFactory.make("identity", f"identity_timestamp-{camera_id}"))
    pipeline.add(identity_timestamp)
    src.connect("pad-added", on_pad_added, (identity_timestamp, pipeline))

    depay = check_elem("rtph265depay", Gst.ElementFactory.make("rtph265depay", f"depay-{camera_id}"))
    h265parse = check_elem("h265parse", Gst.ElementFactory.make("h265parse", f"h265parse-{camera_id}"))
    h265parse.set_property("config-interval", 2)
    tee = check_elem("tee", Gst.ElementFactory.make("tee", f"tee-{camera_id}"))

    # --- Recording Branch (H265) ---
    queue_rec = check_elem("queue", Gst.ElementFactory.make("queue", f"queue_record-{camera_id}"))
    queue_rec.set_property("max-size-buffers", 300)
    rec_parse = check_elem("h265parse", Gst.ElementFactory.make("h265parse", f"record_parse-{camera_id}"))
    rec_parse.set_property("config-interval", 1)
    rec_parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, log_timestamp_probe)
    mux = check_elem("mpegtsmux", Gst.ElementFactory.make("mpegtsmux", f"muxer-{camera_id}"))
    fsink = check_elem("filesink", Gst.ElementFactory.make("filesink", f"fsink-{camera_id}"))
    fsink.set_property("location", f"recorded_test_{camera_id}.ts") #Unique names
    fsink.set_property("sync", False)
    fsink.set_property("async", False)

    # --- WebRTC Branch (Transcoding H265 to H264) ---
    queue_webrtc = check_elem("queue", Gst.ElementFactory.make("queue", f"queue_webrtc-{camera_id}"))
    queue_webrtc.set_property("max-size-buffers", 300)
    queue_webrtc.set_property("leaky", 1)
    decoder = check_elem("avdec_h265", Gst.ElementFactory.make("avdec_h265", f"decoder-{camera_id}"))
    encoder = check_elem("x264enc", Gst.ElementFactory.make("x264enc", f"encoder-{camera_id}"))
    encoder.set_property("key-int-max", 10)  # Force keyframe every 10 frames.
    encoder.set_property("tune", "zerolatency")
    h264parse_web = check_elem("h264parse", Gst.ElementFactory.make("h264parse", f"h264parse_web-{camera_id}"))
    h264parse_web.set_property("config-interval", 1)  # Insert SPS/PPS with every keyframe.
    pay = check_elem("rtph264pay", Gst.ElementFactory.make("rtph264pay", f"pay-{camera_id}"))
    pay.set_property("pt", 96)
    capsfilter = check_elem("capsfilter", Gst.ElementFactory.make("capsfilter", f"capsfilter-{camera_id}"))
    caps_text = "application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96"
    caps = Gst.Caps.from_string(caps_text)
    capsfilter.set_property("caps", caps)
    # Add a debug probe to log buffers arriving at the capsfilter sink continuously.
    capsfilter.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, debug_probe)
  

    webrtc = check_elem("webrtcbin", Gst.ElementFactory.make("webrtcbin", f"webrtcbin-{camera_id}"))  # Unique name
    webrtc.set_property("stun-server", "stun://stun.l.google.com:19302")
    # now add your TURN server
    webrtc.emit(
        "add-turn-server",
        "turn://USERNAME:PASSWORD@your.turn.host:3478?transport=udp"
    )

    webrtc.initial_mid = None

    # Add all elements to the pipeline.
    for elem in (src, identity_timestamp, depay, h265parse, tee, queue_rec,
                 rec_parse, mux, fsink, queue_webrtc, decoder, encoder,
                 h264parse_web, pay, capsfilter, webrtc):
        if elem is not None and pipeline.get_by_name(elem.get_name()) is None:
            pipeline.add(elem)

    # --- Linking Branches ---
    # Common branch: identity -> depay -> h265parse -> tee.
    if not link_many(identity_timestamp, depay, h265parse, tee):
        raise RuntimeError(f"[FATAL] Failed to link the primary video branch for camera {camera_id}.")

    # Recording branch: tee -> queue_rec -> rec_parse -> mux -> fsink.
    tee_src_pad = tee.get_request_pad("src_%u")
    queue_rec_sink_pad = queue_rec.get_static_pad("sink")

    if tee_src_pad.link(queue_rec_sink_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError(f"[FATAL] Failed to link tee to recording queue for camera {camera_id}.")

    if not link_many(queue_rec, rec_parse):
        raise RuntimeError(f"[FATAL] Failed to link recording branch: queue_rec -> rec_parse for camera {camera_id}")

    video_src_pad = rec_parse.get_static_pad("src")
    video_pad = mux.get_request_pad("sink_%d")

    if not video_src_pad or not video_pad:
        raise RuntimeError(f"[FATAL] Could not retrieve pads for video linking for camera {camera_id}.")

    if video_src_pad.link(video_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError(f"[FATAL] Failed to link video stream to mux for camera {camera_id}.")

    if not link_many(mux, fsink):
        raise RuntimeError(f"[FATAL] Failed to link mux to filesink for camera {camera_id}.")

    # WebRTC branch: tee -> queue_webrtc -> decoder -> encoder -> h264parse_web -> pay -> capsfilter -> webrtcbin.
    tee_webrtc_pad = tee.get_request_pad("src_%u")

    if tee_webrtc_pad is None:
        raise RuntimeError(f"[FATAL] Failed to get request pad from tee for WebRTC branch for camera {camera_id}.")

    if tee_webrtc_pad.link(queue_webrtc.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
        raise RuntimeError(f"[FATAL] Failed to link tee to webrtc queue for camera {camera_id}.")

    if not link_many(queue_webrtc, decoder, encoder, h264parse_web, pay):
        raise RuntimeError(f"[FATAL] Failed to link WebRTC branch (queue_webrtc -> decoder -> encoder -> h264parse_web -> pay) for camera {camera_id}")

    pay.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, rtp_probe)

    h264parse_web.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, lambda pad, info: keyframe_probe(pad, info, camera_id))  # Pass camera_id
    if not link_many(pay, capsfilter):
        raise RuntimeError("[FATAL] Failed to link pay → capsfilter")
    webrtc_sink = webrtc.get_request_pad("sink_%u")
    if not webrtc_sink:
        raise RuntimeError(f"[FATAL] Could not get request pad from webrtcbin for camera {camera_id}")
    if capsfilter.get_static_pad("src").link(webrtc_sink) != Gst.PadLinkReturn.OK:
        raise RuntimeError(f"[FATAL] Failed to link capsfilter → webrtcbin request pad for camera {camera_id}")

    print(f"[INFO] Pipeline created for camera {camera_id}.")
    return pipeline, webrtc

#############################################
# WebRTC and Signaling Callbacks
#############################################

def on_negotiation_needed(element, camera_id):  # Added camera_id
    print(f"[DEBUG] on-negotiation-needed called for camera {camera_id}; initiating offer creation.")
    promise = Gst.Promise.new_with_change_func(on_offer_created, element, camera_id)  # Pass camera_id
    element.emit("create-offer", None, promise)

def on_offer_created(promise, element, camera_id):  # Added camera_id
    global ws, cameras

    reply = promise.get_reply()
    offer = reply.get_value("offer")
    element.emit("set-local-description", offer, None)
    sdp_text = offer.sdp.as_text()

    if element.initial_mid is None:
        for line in sdp_text.splitlines():
            if line.startswith("a=mid:"):
                element.initial_mid = line.split(":")[1]
                print(f"[DEBUG] Storing initial a=mid: {element.initial_mid}")
                break

    # Modify SDP to keep MID consistent
    if element.initial_mid is not None:
        new_sdp_text = ""
        for line in sdp_text.splitlines():
            if line.startswith("a=mid:"):
                new_sdp_text += f"a=mid:{element.initial_mid}\r\n"  # Keep the original MID
            else:
                new_sdp_text += line + "\r\n"
        sdp_text = new_sdp_text
        print("[DEBUG] Modified SDP to keep a=mid consistent.")
    else:
        print("[WARN] Initial a=mid not found. Cannot modify SDP.")


    print("[DEBUG] Local SDP offer (after renegotiation):")
    print(sdp_text)
    print("[DEBUG] Preparing to send offer over WebSocket...")
    if ws is not None:
        main_loop.call_soon_threadsafe(asyncio.ensure_future, send_sdp_offer(camera_id, sdp_text))
    else:
        print("[WARN] WebSocket not connected. Storing offer for later send.")
        pending_offer_sdp = sdp_text

async def send_sdp_offer(camera_id, sdp_text):
    message = json.dumps({"type": "offer", "stream_id": camera_id, "sdp": sdp_text})
    if ws and ws.open:
        await ws.send(message)
        print(f"[INFO] Sent SDP offer for camera {camera_id}")
    else:
        print("[ERROR] WebSocket not connected.")

def on_ice_candidate(element, mlineindex, candidate, camera_id):  # Added camera_id
    print(f"[DEBUG] ICE candidate found for camera {camera_id}: {candidate}")
    main_loop.call_soon_threadsafe(asyncio.ensure_future,
                                    send_ice_candidate(camera_id, mlineindex, candidate))  # Pass camera_id

async def send_ice_candidate(camera_id, mlineindex, candidate):  # Added camera_id
    message = json.dumps({"type": "ice", "stream_id": camera_id, "candidate": candidate, "sdpMLineIndex": mlineindex})
    if ws and ws.open:
        await ws.send(message)
        print(f"[INFO] Sent ICE candidate for camera {camera_id}")
    else:
        print("[ERROR] WebSocket not connected.")

#############################################
# WebSocket Handling
#############################################

async def signaling_handler(websocket, path):
    global ws
    ws = websocket
    print("[INFO] WebSocket connected.")
    try:
        async for message in websocket:
            try:
                msg = json.loads(message)
                stream_id = msg.get("stream_id")  # Get stream_id from the message

                if msg["type"] == "register":
                    print(f"[INFO] Client registered for stream {stream_id}")
                    # send the first SDP offer for this camera
                    trigger_renegotiation_if_needed(stream_id)
                elif msg["type"] == "answer":
                    # Client answered our offer
                    sdp = msg["sdp"]
                    print(f"[DEBUG] Received SDP answer for stream {stream_id}")
                    if stream_id in cameras:
                        wb = cameras[stream_id]["webrtc"]
                        # parse and apply the answer
                        sdp_msg = GstSdp.SDPMessage.new_from_text(sdp)[1]
                        wb.emit("set-remote-description",
                                GstWebRTC.WebRTCSessionDescription.new(
                                    GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg),
                                None)
                        print("Mohit")
                    else:
                        print(f"[WARN] No pipeline found for stream ID: {stream_id}")
                elif msg["type"] == "ice":
                    candidate = msg["candidate"]
                    print("Mohit")
                    print(msg)
                    sdpmlineindex = msg["sdpMLineIndex"]
                    print("Mohit------------------------------------------------------------------------------------")
 
                    print(f"[DEBUG] Received ICE candidate from client for stream {stream_id}: {candidate[:20]}...")
                    if candidate is None or sdpmlineindex is None:
                        print(f"[WARN] Skipping malformed ICE: {msg}")
                        continue
                    if stream_id in cameras:
                        webrtcbin = cameras[stream_id]["webrtc"]
                        webrtcbin.emit("add-ice-candidate", sdpmlineindex, candidate)
                        print("add-ice-candidate emitted")
                    else:
                        print(f"[WARN] No pipeline found for stream ID: {stream_id}")
                else:
                    print(f"[DEBUG] Unknown message type: {msg['type']}")

            except json.JSONDecodeError:
                print("[ERROR] Invalid JSON format.")
            except Exception as e:
                print(f"[ERROR] Error processing message: {e}")

    except websockets.exceptions.ConnectionClosedOK:
        print("[INFO] WebSocket connection closed.")
    finally:
        print("[INFO] Cleaning up WebSocket.")
        ws = None  # Reset WebSocket connection

#############################################
# Main Function
#############################################

async def main():
    global main_loop, cameras
    Gst.init(None)
    main_loop = asyncio.get_running_loop()
    start_server = websockets.serve(signaling_handler, "localhost", 8765)
    print("[INFO] Starting WebSocket server...")

    # Example usage: Adding multiple cameras
    camera_configs = [
        {"camera_id": "camera1", "camera_url": "rtsp://admin:Password@10.0.0.4:554/Streaming/Channels/101"},
        {"camera_id": "camera2", "camera_url": "rtsp://admin:Password@10.0.0.4:554/Streaming/Channels/101"},
        {"camera_id": "camera3", "camera_url": "rtsp://admin:Password@10.0.0.4:554/Streaming/Channels/101"},
        {"camera_id": "camera4", "camera_url": "rtsp://admin:Password@10.0.0.4:554/Streaming/Channels/101"}



    ]

    for config in camera_configs:
        camera_id = config["camera_id"]
        camera_url = config["camera_url"]
        add_camera(camera_id, camera_url)
        pipeline = cameras[camera_id]["pipeline"]
        webrtcbin = cameras[camera_id]["webrtc"]

        # Connect signaling for EACH webrtcbin
        webrtcbin.connect("on-negotiation-needed", on_negotiation_needed, camera_id)
        webrtcbin.connect("on-ice-candidate", on_ice_candidate, camera_id)

        pipeline.set_state(Gst.State.PLAYING)
        print(f"[INFO] Pipeline started for camera {camera_id}.")

    try:
        await start_server
        await asyncio.Future()  # Keep the server running
    except KeyboardInterrupt:
        print("[INFO] Shutting down...")
        for camera_id in list(cameras.keys()):
            remove_camera(camera_id)
        print("[INFO] Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
