"""Camera perception for Wastraq - phase 1: RealSense picker tracking.

    RealSense (RGB + aligned depth)
        -> YOLO person detection
        -> ByteTrack persistent track ids
        -> robust depth at a ground-contact anchor
        -> deprojection to CAMERA-LOCAL metres (x right, z forward)
        -> smoothing + a short trajectory buffer
        -> /vision/* and the live debug page

This stops at camera-local coordinates on purpose. There is no vehicle and no
GNSS yet, so there is no honest world transform to make - and inventing a
latitude/longitude here would be exactly the "nearest GPS point" shortcut the
whole property-association design exists to avoid.

Importing this package pulls in NO hardware or ML dependency. pyrealsense2,
ultralytics, cv2 and numpy are imported inside the camera thread, so a
machine without them still serves the property system normally and simply
reports camera_connected=false.
"""

from .api import router

__all__ = ["router"]
