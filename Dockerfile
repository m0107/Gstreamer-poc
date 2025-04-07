FROM jrottenberg/ffmpeg:latest

COPY video1.mp4 /video1.mp4

ENTRYPOINT ["ffmpeg", "-stream_loop", "-1", "-re", "-i", "/video1.mp4", "-c", "copy", "-f", "rtsp", "rtsp://rtsp-server:8554/mystream"]