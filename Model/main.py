"""
This Codes Temporarily Used for Develop Convenient Purpose.
Not Used from 01Feb2023 because of Server Integration.
"""

from typing import List
from fastapi import FastAPI, UploadFile, File, Request, Response
from detector import detect

app = FastAPI()


@app.post("/")
async def rsp(request: Request, files: List[UploadFile] = File(...)):
    for file in files:
        # file_name = request.headers["filename"]
        # user_id = requests.headers["user_id"]
        file_name = file.filename
        user_id = request.headers.get('user_id')
        video_bytes = await file.read()
        with open(file_name, 'wb') as f:
            f.write(video_bytes)
        result, result_json = detect(file_name, f'encoded_{file_name}')
        result_b = None
        with open(result, 'rb') as f:
            result_b = f.read()
        # with open(result_json,'r') as f:
        #    result_json_b = f.read()
        return Response(content=result_b, media_type="video/mp4", headers={"user_id": user_id, "filename": file_name})
        # return Response(content=result_b, data=result_json, media_type="video/mp4", headers={"user_id": user_id,"filename": file_name})
