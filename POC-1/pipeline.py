import os
import uuid
import threading
import logging
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gst, GObject

# Initialize GStreamer
Gst.init(None)

# Load credentials from environment (set ONVIF_USER, ONVIF_PASSWORD)
USER = os.getenv("ONVIF_USER")
PASS = os.getenv("ONVIF_PASSWORD")

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)

class StreamPipeline:
    """Wraps a GStreamer pipeline for WebRTC streaming and optional H.264 recording."""
    def __init__(self, rtsp_uri: str, stun_server: str, record: bool = True):
        self.rtsp = rtsp_uri
        self.stun = stun_server
        self.record_enabled = record
        self.id = str(uuid.uuid4())
        self.loop = GObject.MainLoop()

        logger.info(f"[{self.id}] Initializing pipeline")
        logger.debug(f"[{self.id}] RTSP URI: {self.rtsp}")
        logger.debug(f"[{self.id}] STUN server: {self.stun}")
        logger.debug(f"[{self.id}] Record enabled: {self.record_enabled}")

        self.pipeline = self._build_pipeline()
        logger.info(f"[{self.id}] Pipeline built successfully")

    def _build_pipeline(self) -> Gst.Pipeline:
        """
        Builds a GStreamer pipeline:
          - Fetch H.265 from RTSP (with credentials)
          - Split via tee:
            * Record MP4 with H.264
            * Decode → H.264 → RTP → webrtcbin
        """
        pipeline = Gst.Pipeline.new(f"pipeline_{self.id}")
        logger.debug(f"[{self.id}] Created Gst.Pipeline")

        # Source and depayload
        src = Gst.ElementFactory.make("rtspsrc", "src")
        if not src:
            logger.error(f"[{self.id}] Failed to create rtspsrc element")
            raise RuntimeError("Could not create rtspsrc")
        src.set_property("location", self.rtsp)
        src.set_property("protocols", "tcp")
        src.set_property("latency", 200)
        if USER and PASS:
            src.set_property("user-id", USER)
            src.set_property("user-pw", PASS)
            logger.debug(f"[{self.id}] Set RTSP credentials on rtspsrc")
        else:
            logger.warning(f"[{self.id}] RTSP credentials not set (ONVIF_USER/PASSWORD missing)")

        depay = Gst.ElementFactory.make("rtph265depay", "depay")
        tee = Gst.ElementFactory.make("tee", "tee")
        for elem, name in [(depay, "rtph265depay"), (tee, "tee")]:
            if not elem:
                logger.error(f"[{self.id}] Failed to create {name} element")
                raise RuntimeError(f"Could not create {name}")
        logger.debug(f"[{self.id}] Created depay and tee elements")

        # Add to pipeline and link src -> depay -> tee
        pipeline.add(src)
        pipeline.add(depay)
        pipeline.add(tee)
        src.connect("pad-added", self._on_pad_added, depay)
        if not depay.link(tee):
            logger.error(f"[{self.id}] Failed to link depay -> tee")
            raise RuntimeError("depay -> tee link failure")
        logger.debug(f"[{self.id}] Linked depay -> tee")

        # Recording branch (MP4 with H.264)
        if self.record_enabled:
            rec_queue = Gst.ElementFactory.make("queue", "rec_queue")
            rec_parse = Gst.ElementFactory.make("h265parse", "rec_parse")  # Parse H.265
            decoder = Gst.ElementFactory.make("avdec_h265", "decoder")  # Decode H.265
            encoder = Gst.ElementFactory.make("x264enc", "encoder")  # Encode to H.264
            mux = Gst.ElementFactory.make("qtmux", "mux")  # MP4 Muxer (qtmux)
            mux.set_property("reserved-moov-update-period", 1)  # Required for robust muxing

            rec_sink = Gst.ElementFactory.make("filesink", f"rec_sink_{self.id}")
            rec_sink.set_property("location", f"record_{self.id}.mp4")

            for elem, name in [(rec_queue, "queue"), (rec_parse, "h265parse"), (decoder, "avdec_h265"),
                               (encoder, "x264enc"), (mux, "qtmux"), (rec_sink, "filesink")]:
                if not elem:
                    logger.error(f"[{self.id}] Failed to create {name} for recording branch")
                    raise RuntimeError(f"Could not create {name}")

            pipeline.add(rec_queue)
            pipeline.add(rec_parse)
            pipeline.add(decoder)
            pipeline.add(encoder)
            pipeline.add(mux)
            pipeline.add(rec_sink)

            if not tee.link(rec_queue):
                logger.error(f"[{self.id}] Failed to link tee -> rec_queue")
                raise RuntimeError("tee -> rec_queue link failure")
            if not rec_queue.link(rec_parse):
                logger.error(f"[{self.id}] Failed to link rec_queue -> rec_parse")
                raise RuntimeError("rec_queue -> rec_parse link failure")
            if not rec_parse.link(decoder):
                logger.error(f"[{self.id}] Failed to link rec_parse -> decoder")
                raise RuntimeError("rec_parse -> decoder link failure")
            if not decoder.link(encoder):
                logger.error(f"[{self.id}] Failed to link decoder -> encoder")
                raise RuntimeError("decoder -> encoder link failure")
            if not encoder.link(mux):
                logger.error(f"[{self.id}] Failed to link encoder -> mux")
                raise RuntimeError("encoder -> mux link failure")
            if not mux.link(rec_sink):
                logger.error(f"[{self.id}] Failed to link mux -> rec_sink")
                raise RuntimeError("mux -> rec_sink link failure")

            logger.info(f"[{self.id}] Recording branch linked: MP4 with H.264 to file")

        # WebRTC branch (H.264)
        wr_queue = Gst.ElementFactory.make("queue", "wr_queue")
        h265parse = Gst.ElementFactory.make("h265parse", "h265parse_webrtc")
        decoder = Gst.ElementFactory.make("avdec_h265", "decoder_webrtc")
        convert = Gst.ElementFactory.make("videoconvert", "convert")
        h264enc = Gst.ElementFactory.make("x264enc", "h264enc")  # Encode to H.264
        rtph264pay = Gst.ElementFactory.make("rtph264pay", "rtph264pay")  # RTP H.264 payloader
        webrtc = Gst.ElementFactory.make("webrtcbin", "webrtc")

        if not all([wr_queue, h265parse, decoder, convert, h264enc, rtph264pay, webrtc]):
            logger.error(f"[{self.id}] Failed to create one or more WebRTC branch elements")
            raise RuntimeError("WebRTC branch element creation failed")

        webrtc.set_property("stun-server", self.stun)

        pipeline.add(wr_queue)
        pipeline.add(h265parse)
        pipeline.add(decoder)
        pipeline.add(convert)
        pipeline.add(h264enc)
        pipeline.add(rtph264pay)
        pipeline.add(webrtc)

        # Link tee -> wr_queue -> h265parse -> decoder -> convert -> h264enc -> rtph264pay -> webrtc
        if not tee.link(wr_queue):
            logger.error(f"[{self.id}] Failed to link tee -> wr_queue")
            raise RuntimeError("tee -> wr_queue link failure")
        if not wr_queue.link(h265parse):
            logger.error(f"[{self.id}] Failed to link wr_queue -> h265parse")
            raise RuntimeError("wr_queue -> h265parse link failure")
        if not h265parse.link(decoder):
            logger.error(f"[{self.id}] Failed to link h265parse -> decoder")
            raise RuntimeError("h265parse -> decoder link failure")
        if not decoder.link(convert):
            logger.error(f"[{self.id}] Failed to link decoder -> convert")
            raise RuntimeError("decoder -> convert link failure")
        if not convert.link(h264enc):
            logger.error(f"[{self.id}] Failed to link convert -> h264enc")
            raise RuntimeError("convert -> h264enc link failure")
        if not h264enc.link(rtph264pay):
            logger.error(f"[{self.id}] Failed to link h264enc -> rtph264pay")
            raise RuntimeError("h264enc -> rtph264pay link failure")

        pay_src = rtph264pay.get_static_pad("src")
        webrtc_sink = webrtc.get_request_pad("sink_%u")
        if not webrtc_sink or pay_src.link(webrtc_sink) != Gst.PadLinkReturn.OK:
            logger.error(f"[{self.id}] Failed to link payloader -> webrtcbin")
            raise RuntimeError("payloader -> webrtc link failure")

        # Add a pad probe to log data flow into webrtcbin
        webrtc_sink.add_probe(Gst.PadProbeType.BUFFER, self._on_webrtc_data, None)
        logger.info(f"[{self.id}] WebRTC branch linked: H.264 → webrtcbin")

        return pipeline

    def _on_pad_added(self, src, pad, target):
        caps = pad.get_current_caps()
        if not caps:
            logger.warning(f"[{self.id}] Pad {pad.get_name()} has no caps, cannot link")
            return

        structure = caps.get_structure(0)
        if not structure:
            logger.warning(f"[{self.id}] Caps on pad {pad.get_name()} have no structure, cannot link")
            return

        name = structure.get_name()
        logger.debug(f"[{self.id}] pad-added: {pad.get_name()} caps={name}")
        if name.startswith("application/x-rtp"):
            ret = pad.link(target.get_static_pad("sink"))
            logger.debug(f"[{self.id}] Linked pad {pad.get_name()} to depay sink: {ret}")

    def _on_webrtc_data(self, pad, info, user_data):
        buffer = info.get_buffer()
        if buffer:
            logger.info(f"[{self.id}] Data is flowing into webrtcbin. Buffer size: {buffer.get_size()} bytes")
        else:
            logger.warning(f"[{self.id}] No data flowing into webrtcbin!")
        return Gst.PadProbeReturn.OK

    def start(self):
        logger.info(f"[{self.id}] Starting pipeline")
        self.pipeline.set_state(Gst.State.PLAYING)
        threading.Thread(target=self.loop.run, daemon=True).start()
        logger.info(f"[{self.id}] Stream started.")

    def stop(self):
        logger.info(f"[{self.id}] Stopping pipeline")
        self.pipeline.set_state(Gst.State.NULL)
        self.loop.quit()
        logger.info(f"[{self.id}] Stream stopped.")

if __name__ == "__main__":
    import time
    TEST_RTSP = "rtsp://admin:Password@10.0.0.4:554/Streaming/Channels/101"
    sp = StreamPipeline(TEST_RTSP, "stun://stun.l.google.com:19302")
    sp.start()
    time.sleep(8)
    sp.stop()
    print(f"Recorded raw H.265 to record_{sp.id}.mp4")
