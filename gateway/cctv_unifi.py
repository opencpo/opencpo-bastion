"""
OpenCPO Bastion — UniFi Protect Adapter (standalone module)

Re-exports UniFiProvider from cctv.py for clarity and direct import.
All implementation lives in cctv.py to avoid duplication.

Use this module when you want to instantiate a UniFi provider directly:

    from gateway.cctv_unifi import UniFiProvider
    provider = UniFiProvider(config)
    await provider.connect()
    cameras = await provider.discover()
"""

from gateway.cctv import UniFiProvider, Camera, CameraEvent, CameraProvider

__all__ = ["UniFiProvider", "Camera", "CameraEvent", "CameraProvider"]
