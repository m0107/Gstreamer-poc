# file: Dockerfile
# Use Ubuntu 22.04 as base, which ships with Python 3.10 and GI bindings
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# 1) Install system packages: Python, pip, GStreamer, GI, and ICE support
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-gi \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libnice10 \
 && rm -rf /var/lib/apt/lists/*

# 2) Create a symlink so 'python' refers to Python 3
RUN ln -sf /usr/bin/python3 /usr/bin/python

# 3) Install Python dependencies
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 4) Copy application code
COPY . .

# 5) Expose API port and run
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]