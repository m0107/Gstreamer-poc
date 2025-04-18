# file: pipeline.py
import uuid
import threading
import logging
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
          - Reads H264 from RTSP
          - Splits into two branches:
              * Records to an MPEG-TS file
              * Streams over WebRTC via webrtcbin
        """
        launch = (
            f"rtspsrc location={self.rtsp} protocols=tcp latency=200 name=src "
            # Depayload the incoming RTP H264 into raw H264 bytestream
            "src. ! rtph264depay name=depay "
            # Split into two branches
            "depay. ! tee name=t "
            # Branch 1: recording
            f"t. ! queue ! h264parse ! mpegtsmux ! filesink location=record_{self.id}.ts sync=false "
            # Branch 2: WebRTC
            f"t. ! queue ! h264parse ! rtph264pay pt=96 config-interval=1 ! "
            "application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96 ! "
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
        GObject.threads_init()
        self.loop.run()

    def stop(self):
        """Stop pipeline and quit the main loop."""
        self.pipeline.set_state(Gst.State.NULL)
        self.loop.quit()
        logger.info(f"Stream {self.id} stopped.")
