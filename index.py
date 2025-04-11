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
CAMERA_URL = "rtsp://admin:Password@192.168.0.201:554/Streaming/channels/101"

# Globals
pipeline = None
webrtcbin = None
ws = None
main_loop = None
pending_offer_sdp = None
start_time = None  # For forced timestamping (if needed)

#############################################
# Keyframe Probe Function for Renegotiation #
#############################################
def keyframe_probe(pad, info):
    # This probe runs on h264parse_web's src pad.
    # We trigger renegotiation when the first keyframe appears,
    # i.e. a buffer that is NOT marked as a delta unit.
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        if buf and not (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT):
            print("[DEBUG] Keyframe detected, triggering renegotiation.")
            trigger_renegotiation_if_needed()
            return Gst.PadProbeReturn.REMOVE  # Remove the probe after triggering.
    return Gst.PadProbeReturn.OK

#############################################
# Renegotiation Trigger Function            #
#############################################
def trigger_renegotiation_if_needed():
    global webrtcbin, ws, main_loop
    if webrtcbin is None:
        print("[DEBUG] webrtcbin is not set; skipping renegotiation.")
        return
    print("[DEBUG] Triggering renegotiation now.")
    promise = Gst.Promise.new_with_change_func(on_offer_created, webrtcbin, None)
    webrtcbin.emit("create-offer", None, promise)

#############################################
# Probes and Utility Functions              #
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

def log_timestamp_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        # Uncomment for detailed timestamp logging:
        # if buf:
        #     print(f"[TIMESTAMP LOG] PTS: {buf.pts}, DTS: {buf.dts}, Size: {buf.get_size()} bytes")
    return Gst.PadProbeReturn.OK

def log_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        buf = info.get_buffer()
        # Uncomment for detailed buffer logging:
        # if buf:
        #     print(f"[LOG PROBE] Buffer at {pad.get_name()}; size: {buf.get_size()} bytes, PTS: {buf.pts}")
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

def on_pad_added(src, new_pad, target_elem):
    """
    Link the dynamic pad from the RTSP source:
      - If the pad's caps is "application/x-rtp", link it to the target element.
      - Otherwise, link it to a fakesink.
    """
    print(f"[DEBUG] on_pad_added: Detected new pad {new_pad.get_name()} from {src.get_name()}")
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

    # For RTP pads, link it to the target element.
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
        # We no longer use a fixed timer here; instead we use a probe on the transcoding branch.
    else:
        print(f"[ERROR] Failed to link dynamic pad {new_pad.get_name()} (error code: {ret}).")

def link_many(*elements):
    for i in range(len(elements) - 1):
        if not elements[i].link(elements[i + 1]):
            print(f"[ERROR] Failed to link {elements[i].get_name()} -> {elements[i + 1].get_name()}")
            return False
        print(f"[DEBUG] Linked {elements[i].get_name()} -> {elements[i + 1].get_name()}")
    return True

def link_webrtc_branch(pad, info, capsfilter, webrtc):
    if info.get_event().type == Gst.EventType.CAPS:
        print("[DEBUG] CAPS detected in WebRTC branch; linking payloader to webrtcbin...")
        pad.link(capsfilter.get_static_pad("sink"))
        capsfilter.get_static_pad("src").link(webrtc.get_request_pad("sink_%u"))
        print("[INFO] Linked RTP payloader -> capsfilter -> webrtcbin")
        return Gst.PadProbeReturn.REMOVE
    return Gst.PadProbeReturn.OK

#############################################
# Pipeline Creation
#############################################
def create_pipeline():
    global pipeline, webrtcbin
    print("[INFO] Creating pipeline...")
    pipeline = Gst.Pipeline.new("vms-pipeline")
    audio_present = has_audio_stream(CAMERA_URL)
    print(f"[INFO] Audio present in stream: {audio_present}")

    # --- Common Video Branch ---
    src = check_elem("rtspsrc", Gst.ElementFactory.make("rtspsrc", "src"))
    src.set_property("location", CAMERA_URL)
    src.set_property("latency", 300)
    
    identity_timestamp = check_elem("identity", Gst.ElementFactory.make("identity", "identity_timestamp"))
    pipeline.add(identity_timestamp)
    src.connect("pad-added", on_pad_added, identity_timestamp)
    # Optionally, force timestamping:
    # identity_timestamp.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, force_timestamp_probe)

    # Process H265 from the camera (common branch).
    depay = check_elem("rtph265depay", Gst.ElementFactory.make("rtph265depay", "depay"))
    h265parse = check_elem("h265parse", Gst.ElementFactory.make("h265parse", "h265parse"))
    h265parse.set_property("config-interval", 2)
    tee = check_elem("tee", Gst.ElementFactory.make("tee", "tee"))

    # --- Recording Branch (H265) ---
    queue_rec = check_elem("queue", Gst.ElementFactory.make("queue", "queue_record"))
    queue_rec.set_property("max-size-buffers", 300)
    
    rec_parse = check_elem("h265parse", Gst.ElementFactory.make("h265parse", "record_parse"))
    rec_parse.set_property("config-interval", 1)
    rec_parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, log_timestamp_probe)
    
    mux = check_elem("mpegtsmux", Gst.ElementFactory.make("mpegtsmux", "muxer"))
    fsink = check_elem("filesink", Gst.ElementFactory.make("filesink", "fsink"))
    fsink.set_property("location", "recorded_test.ts")
    fsink.set_property("sync", False)
    fsink.set_property("async", False)

    # --- WebRTC Branch (Transcoding H265 to H264) ---
    queue_webrtc = check_elem("queue", Gst.ElementFactory.make("queue", "queue_webrtc"))
    queue_webrtc.set_property("max-size-buffers", 300)
    queue_webrtc.set_property("leaky", 1)

    # Transcoding: decode H265, then encode to H264.
    decoder = check_elem("avdec_h265", Gst.ElementFactory.make("avdec_h265", "decoder"))
    encoder = check_elem("x264enc", Gst.ElementFactory.make("x264enc", "encoder"))
    encoder.set_property("key-int-max", 10)  # Force keyframe every 10 frames.
    encoder.set_property("tune", "zerolatency")
    
    h264parse_web = check_elem("h264parse", Gst.ElementFactory.make("h264parse", "h264parse_web"))
    h264parse_web.set_property("config-interval", 1)  # Insert SPS/PPS with every keyframe.
    # Do not set unsupported properties here.
    
    pay = check_elem("rtph264pay", Gst.ElementFactory.make("rtph264pay", "pay"))
    pay.set_property("pt", 96)
    
    capsfilter = check_elem("capsfilter", Gst.ElementFactory.make("capsfilter", "capsfilter"))
    caps_text = "application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96"
    caps = Gst.Caps.from_string(caps_text)
    capsfilter.set_property("caps", caps)
    
    webrtc = check_elem("webrtcbin", Gst.ElementFactory.make("webrtcbin", "webrtcbin"))
    webrtc.set_property("stun-server", "stun://stun.l.google.com:19302")

    # Add all elements to the pipeline.
    for elem in (src, identity_timestamp, depay, h265parse, tee, queue_rec,
                 rec_parse, mux, fsink, queue_webrtc, decoder, encoder,
                 h264parse_web, pay, capsfilter, webrtc):
        if elem is not None and pipeline.get_by_name(elem.get_name()) is None:
            pipeline.add(elem)

    # --- Linking Branches ---
    # Common branch: identity -> depay -> h265parse -> tee.
    if not link_many(identity_timestamp, depay, h265parse, tee):
        raise RuntimeError("[FATAL] Failed to link the primary video branch.")

    # Recording branch: tee -> queue_rec -> rec_parse -> mux -> fsink.
    tee_src_pad = tee.get_request_pad("src_%u")
    queue_rec_sink_pad = queue_rec.get_static_pad("sink")
    if tee_src_pad.link(queue_rec_sink_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError("[FATAL] Failed to link tee to recording queue.")
    if not link_many(queue_rec, rec_parse):
        raise RuntimeError("[FATAL] Failed to link recording branch: queue_rec -> rec_parse")
    video_src_pad = rec_parse.get_static_pad("src")
    video_pad = mux.get_request_pad("sink_%d")
    if not video_src_pad or not video_pad:
        raise RuntimeError("[FATAL] Could not retrieve pads for video linking.")
    if video_src_pad.link(video_pad) != Gst.PadLinkReturn.OK:
        raise RuntimeError("[FATAL] Failed to link video stream to mux.")
    if not link_many(mux, fsink):
        raise RuntimeError("[FATAL] Failed to link mux to filesink.")

    # WebRTC branch: tee -> queue_webrtc -> decoder -> encoder -> h264parse_web -> pay -> capsfilter -> webrtcbin.
    if not link_many(tee, queue_webrtc, decoder, encoder, h264parse_web, pay, capsfilter, webrtc):
        raise RuntimeError("[FATAL] Failed to link WebRTC branch: tee -> queue_webrtc -> decoder -> encoder -> h264parse_web -> pay -> capsfilter -> webrtcbin")
    
    # Install a pad probe on h264parse_web's source pad to trigger renegotiation once a keyframe is detected.
    h264parse_web.get_static_pad("src").add_probe(
        Gst.PadProbeType.BUFFER, keyframe_probe
    )
    
    # Activate the WebRTC branch.
    webrtcbin = webrtc
    print("[INFO] Pipeline created.")
    return pipeline

# ----- WebRTC and Signaling Callbacks -----
def on_negotiation_needed(element):
    print("[DEBUG] on-negotiation-needed called; initiating offer creation.")
    promise = Gst.Promise.new_with_change_func(on_offer_created, element, None)
    element.emit("create-offer", None, promise)

def on_offer_created(promise, _, __):
    global webrtcbin, ws, pending_offer_sdp
    reply = promise.get_reply()
    offer = reply.get_value("offer")
    webrtcbin.emit("set-local-description", offer, None)
    sdp_text = offer.sdp.as_text()
    print("[DEBUG] Local SDP offer (after renegotiation):")
    print(sdp_text)
    print("[DEBUG] Preparing to send offer over WebSocket...")
    if ws is not None:
        main_loop.call_soon_threadsafe(asyncio.ensure_future, send_sdp_offer(sdp_text))
    else:
        print("[WARN] WebSocket not connected. Storing offer for later send.")
        pending_offer_sdp = sdp_text

def on_ice_candidate(element, mlineindex, candidate):
    print(f"[DEBUG] ICE candidate found: {candidate}")
    if ws:
        msg = json.dumps({"type": "ice", "sdpMLineIndex": mlineindex, "candidate": candidate})
        main_loop.call_soon_threadsafe(asyncio.ensure_future, ws.send(msg))
        print("[DEBUG] ICE candidate sent to peer via WebSocket.")

async def send_sdp_offer(sdp_text):
    global ws
    if ws:
        await ws.send(json.dumps({"type": "offer", "sdp": sdp_text}))
        print("[DEBUG] SDP offer sent to remote peer.")
    else:
        print("[WARN] WebSocket not available for sending offer.")

async def handle_signaling():
    global ws, pending_offer_sdp
    try:
        ws = await websockets.connect(SIGNALING_SERVER)
        print(f"[INFO] Connected to signaling server: {SIGNALING_SERVER}")
        if pending_offer_sdp:
            await send_sdp_offer(pending_offer_sdp)
            pending_offer_sdp = None
        async for message in ws:
            print("[DEBUG] WebSocket message received.")
            data = json.loads(message)
            print(data)
            if data.get("type") == "answer":
                try:
                    sdp_result = GstSdp.SDPMessage.new_from_text(data["sdp"])
                except Exception as e:
                    print(f"[ERROR] Exception during SDP creation: {e}")
                    continue
                if isinstance(sdp_result, tuple):
                    _, sdp_msg = sdp_result
                else:
                    sdp_msg = sdp_result
                answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg)
                main_loop.call_soon_threadsafe(lambda: webrtcbin.emit("set-remote-description", answer, None))
                print("[INFO] Remote SDP answer applied.")
            elif data.get("type") == "ice":
                main_loop.call_soon_threadsafe(
                    lambda: webrtcbin.emit("add-ice-candidate", data["sdpMLineIndex"], data["candidate"])
                )
                print(f"[INFO] Remote ICE candidate added: {data['candidate']}")
    except Exception as e:
        print(f"[ERROR] WebSocket signaling error: {e}")
    finally:
        if ws:
            await ws.close()
            print("[INFO] WebSocket connection closed.")

async def monitor_pipeline():
    while True:
        ret, state, pending = pipeline.get_state(Gst.SECOND)
        print(f"[MONITOR] Pipeline state: {state.value_nick} (pending: {pending.value_nick})")
        await asyncio.sleep(60)

async def monitor_file_size(filepath, interval=5):
    while True:
        try:
            size = os.path.getsize(filepath)
            print(f"[FILE MONITOR] {filepath} size: {size} bytes")
        except Exception as e:
            print(f"[FILE MONITOR] Error checking file size: {e}")
        await asyncio.sleep(interval)

async def main():
    global pipeline, webrtcbin, main_loop
    print("[INFO] Application initializing...")
    main_loop = asyncio.get_event_loop()
    pipeline = create_pipeline()

    # Connect WebRTC signals.
    if webrtcbin:
        webrtcbin.connect("on-negotiation-needed", on_negotiation_needed)
        webrtcbin.connect("on-ice-candidate", on_ice_candidate)

    print("[DEBUG] Setting pipeline to PLAYING state...")
    state_ret = pipeline.set_state(Gst.State.PLAYING)
    if state_ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("[FATAL] Failed to set pipeline to PLAYING state.")
    elif state_ret == Gst.StateChangeReturn.ASYNC:
        print("[INFO] Pipeline state change is asynchronous.")
    else:
        print(f"[INFO] Pipeline state change: {state_ret}")

    ret, current, pending = pipeline.get_state(Gst.SECOND * 5)
    print(f"[INFO] Pipeline state result: {ret.value_nick}")
    print(f"[INFO] Pipeline current state: {current.value_nick}, pending: {pending.value_nick}")

    print("[INFO] Pipeline is running. Recording to file active and WebRTC branch enabled.")
    
    # Start signaling and monitoring tasks.
    asyncio.ensure_future(handle_signaling())
    asyncio.ensure_future(monitor_pipeline())
    asyncio.ensure_future(monitor_file_size("recorded_test.ts", interval=60))

    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_bus_message(_, msg):
        try:
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                print(f"[ERROR] GStreamer pipeline error: {err}, debug info: {debug}")
                asyncio.get_event_loop().stop()
            elif msg.type == Gst.MessageType.EOS:
                print("[INFO] GStreamer EOS received. Pipeline will shut down.")
                asyncio.get_event_loop().stop()
            elif msg.type == Gst.MessageType.STATE_CHANGED:
                if msg.src == pipeline:
                    old, new, pending = msg.parse_state_changed()
                    print(f"[DEBUG] Pipeline state changed: {old.value_nick} → {new.value_nick} (pending: {pending.value_nick})")
        except Exception as e:
            print(f"[EXCEPTION] Error in on_bus_message: {e}")

    bus.connect("message", on_bus_message)

    await asyncio.Future()  # Keeps the loop running

if __name__ == "__main__":
    Gst.init(None)
    print("[INFO] GStreamer and gbulb initialized.")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[INFO] Shutdown signal received. Cleaning up...")
    finally:
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
            print("[INFO] Pipeline stopped and cleaned up.")
        print("[INFO] Application terminated.")
