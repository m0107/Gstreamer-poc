# discovery.py

import os
import logging
from urllib.parse import urlparse
from wsdiscovery import WSDiscovery
from onvif import ONVIFCamera
from dotenv import load_dotenv

load_dotenv()
USER = os.getenv("ONVIF_USER")
PASS = os.getenv("ONVIF_PASSWORD")

logger = logging.getLogger(__name__)


def discover_onvif(timeout: int = 5):
    """
    Discover ONVIF devices via WS-Discovery.
    Returns a list of dicts: [{'service_url': ...}, ...]
    """
    wsd = WSDiscovery()
    wsd.start()
    services = wsd.searchServices(timeout=timeout)
    cams = []
    for svc in services:
        xaddrs = svc.getXAddrs()
        if not xaddrs:
            continue
        cams.append({"service_url": xaddrs[0]})
    wsd.stop()
    return cams


def get_rtsp_uri(service_url: str) -> str:
    """
    Get RTSP URI from ONVIF Media Service.

    Requires a local 'wsdl/' folder next to this file containing the ONVIF WSDLs:
      git clone https://github.com/FalkTannhaeuser/python-onvif-zeep.git onvif-zeep
      cp -R onvif-zeep/wsdl ./wsdl
      rm -rf onvif-zeep
    """
    p = urlparse(service_url)
    wsdl_dir = os.path.join(os.path.dirname(__file__), 'wsdl')
    if not os.path.isdir(wsdl_dir):
        raise RuntimeError(f"WSDL directory not found: {wsdl_dir}")

    cam = ONVIFCamera(
        p.hostname,
        p.port or 80,
        USER,
        PASS,
        wsdl_dir,
        service_url
    )
    media = cam.create_media_service()
    profiles = media.GetProfiles()
    token = profiles[0].token
    req = {
        'StreamSetup': {
            'Stream': 'RTP-Unicast',
            'Transport': {'Protocol': 'RTSP'}
        },
        'ProfileToken': token
    }
    res = media.GetStreamUri(req)
    uri = res.Uri
    logger.info(f"RTSP URI for {p.hostname}: {uri}")
    return uri


if __name__ == "__main__":
    import pprint
    logging.basicConfig(level=logging.INFO)
    cams = discover_onvif(timeout=5)
    print("Discovered ONVIF cameras:")
    pprint.pprint(cams)
    if cams:
        rtsp = get_rtsp_uri(cams[0]["service_url"])
        print("\n→ RTSP URI for first camera:\n", rtsp)
    else:
        print("No cameras found.")