"""
This Codes Temporarily Used for Develop Convenient Purpose.
Not Used from 01Feb2023 because of Server Integration.
"""
import cv2

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

application = FastAPI()
video_device_addr = 0


def get_cam_stream(cam=cv2.VideoCapture(video_device_addr)):
    while True:
        success, frame = cam.read()
        if not success:
            break
        ret_val, frame = cv2.imencode(".jpg", frame)
        frame_bin = bytearray(frame.tobytes())
        yield b'--PNPframe\r\n' b'Content-Type: image/jpeg\r\n\r\n' + frame_bin + b'\r\n'


# ==================================== 원본 동영상
@application.get("/original")
def original():
    camera = cv2.VideoCapture(0)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 640)
    return StreamingResponse(get_cam_stream(cam=camera), media_type="multipart/x-mixed-replace; boundary=PNPframe")
