# file: pipeline.py
import uuid
import threading
import logging
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gst, GObject

# Initialize GStreamer
Gst.init(None)
logger = logging.getLogger(__name__)

class StreamPipeline:
    """Wraps a GStreamer pipeline for recording and WebRTC streaming."""
    def __init__(self, rtsp_uri: str, stun_server: str):
        self.rtsp = rtsp_uri
        self.stun = stun_server
        self.id = str(uuid.uuid4())
        self.loop = GObject.MainLoop()
        self.pipeline = self._build_pipeline()

    def _build_pipeline(self) -> Gst.Pipeline:
        """
        Builds a live pipeline that:
          - Reads H.265 from RTSP
          - Splits into two branches:
              * Records to an MPEG-TS file
              * Streams over WebRTC via VP8 encoding
        """
        launch = (
            f"rtspsrc location={self.rtsp} protocols=tcp latency=200 name=src "
            # Depayload H.265
            "src. ! rtph265depay ! h265parse ! decodebin ! videoconvert ! tee name=t "
            # Branch 1: record H.265
            f"t. ! queue ! x265enc tune=zerolatency bitrate=1024 key-int-max=30 "
            "! h265parse ! mpegtsmux ! filesink location=record_{self.id}.ts sync=false "
            # Branch 2: re-encode to VP8 for WebRTC
            "t. ! queue ! vp8enc deadline=1 ! rtpvp8pay pt=96 ! "
            "application/x-rtp,media=video,clock-rate=90000,encoding-name=VP8,payload=96 ! "
            f"webrtcbin name=webrtc stun-server={self.stun}"
        )
        logger.debug(f"Launching pipeline: {launch}")
        return Gst.parse_launch(launch)

    def start(self):
        """Start pipeline and its GLib main loop in a separate thread."""
        self.pipeline.set_state(Gst.State.PLAYING)
        thread = threading.Thread(target=self._run_loop, daemon=True)
        thread.start()
        logger.info(f"Stream {self.id} started.")

    def _run_loop(self):
        # Run the GLib main loop to handle GStreamer messages
        self.loop.run()

    def stop(self):
        """Stop pipeline and quit the main loop."""
        self.pipeline.set_state(Gst.State.NULL)
        self.loop.quit()
        logger.info(f"Stream {self.id} stopped.")


if __name__ == "__main__":
    # quick local smoke test
    import time
    logging.basicConfig(level=logging.INFO)
    TEST_RTSP = "rtsp://admin:Password@10.0.0.9:554/Streaming/Channels/101"
    sp = StreamPipeline(TEST_RTSP, "stun://stun.l.google.com:19302")
    sp.start()
    time.sleep(8)
    sp.stop()
    print(f"Recorded to record_{sp.id}.ts")