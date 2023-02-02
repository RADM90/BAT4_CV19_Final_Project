import tensorrt as trt
import pycuda.autoinit
import pycuda.driver as cuda
import numpy as np
import cv2
import matplotlib.pyplot as plt
import os
from subprocess import check_call
import shlex
import json
import math


def h264_encoding_withgpu(file_path: str, dst_file: str):
    cmd = f"ffmpeg -nostdin -y -i {file_path} -vcodec h264_nvenc -profile:v high -preset slow -pix_fmt yuv420p -src_range 1 -dst_range 1 -g 30 -bf 2 -an -movflags faststart {dst_file}"
    check_call(shlex.split(cmd), universal_newlines=True)


class Colors:
    # Ultralytics color palette https://ultralytics.com/
    def __init__(self):
        # hex = matplotlib.colors.TABLEAU_COLORS.values()
        hexs = ('FF3838', 'FF9D97', 'FF701F', 'FFB21D', 'CFD231', '48F90A', '92CC17', '3DDB86', '1A9334', '00D4BB',
                '2C99A8', '00C2FF', '344593', '6473FF', '0018EC', '8438FF', '520085', 'CB38FF', 'FF95C8', 'FF37C7')
        self.palette = [self.hex2rgb(f'#{c}') for c in hexs]
        self.n = len(self.palette)

    def __call__(self, i, bgr=False):
        c = self.palette[int(i) % self.n]
        return (c[2], c[1], c[0]) if bgr else c

    @staticmethod
    def hex2rgb(h):  # rgb order (PIL)
        return tuple(int(h[1 + i:1 + i + 2], 16) for i in (0, 2, 4))


colors = Colors()  # create instance for 'from utils.plots import colors'


class BaseEngine(object):
    def __init__(self, engine_path):
        self.mean = None
        self.std = None
        self.n_classes = 29
        self.class_names_en = ['bicycle','bus','car','carrier','cat','dog','motorcycle','movable_signage','person',
                'scooter','stroller','truck','wheelchair','barricade','bench','bollard','chair','fire_hydrant',
                'kiosk','parking_meter','pole','potted_plant','power_controller','stop','table','traffic_light',
                'traffic_light_controller','traffic_sign','tree_trunk']

        self.class_names_kr = ['자전거','버스','자동차','손수레','고양이','개','오토바이','간판','사람',
                '스쿠터','유모차','트럭','휠체어','바리케이드','벤치','볼라드','의자','소화전',
                '키오스크','주차요금정산기','기둥','화분','전력제어함','정류장','탁자','신호등',
                '신호등제어기','교통표지판','가로수']
        self.location_names = ['왼쪽','중앙','오른쪽']
        self.warning_names = ['', '가까이','']

        logger = trt.Logger(trt.Logger.WARNING)
        logger.min_severity = trt.Logger.Severity.ERROR
        runtime = trt.Runtime(logger)
        trt.init_libnvinfer_plugins(logger,'') # initialize TensorRT plugins
        with open(engine_path, "rb") as f:
            serialized_engine = f.read()
        engine = runtime.deserialize_cuda_engine(serialized_engine)
        self.imgsz = engine.get_binding_shape(0)[2:]  # get the read shape of model, in case user input it wrong
        self.context = engine.create_execution_context()
        self.inputs, self.outputs, self.bindings = [], [], []
        self.stream = cuda.Stream()
        for binding in engine:
            size = trt.volume(engine.get_binding_shape(binding))
            dtype = trt.nptype(engine.get_binding_dtype(binding))
            host_mem = cuda.pagelocked_empty(size, dtype)
            device_mem = cuda.mem_alloc(host_mem.nbytes)
            self.bindings.append(int(device_mem))
            if engine.binding_is_input(binding):
                self.inputs.append({'host': host_mem, 'device': device_mem})
            else:
                self.outputs.append({'host': host_mem, 'device': device_mem})


    def infer(self, img):
        self.inputs[0]['host'] = np.ravel(img)
        # transfer data to the gpu
        for inp in self.inputs:
            cuda.memcpy_htod_async(inp['device'], inp['host'], self.stream)
        # run inference
        self.context.execute_async_v2(
            bindings=self.bindings,
            stream_handle=self.stream.handle)
        # fetch outputs from gpu
        for out in self.outputs:
            cuda.memcpy_dtoh_async(out['host'], out['device'], self.stream)
        # synchronize stream
        self.stream.synchronize()

        data = [out['host'] for out in self.outputs]
        return data

    def detect_video(self, file_name: str, session_id:str, conf=0.5, end2end=False) -> str:
        cap = cv2.VideoCapture(file_name)
        fourcc = cv2.VideoWriter_fourcc('I','4','2','0')
        fps = int(round(cap.get(cv2.CAP_PROP_FPS)))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        outfilename_raw = 'detectraw.avi'
        out = cv2.VideoWriter(outfilename_raw,fourcc,fps,(width,height))
        fpsout = 0
        subtitles = {}
        import time
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            blob, ratio = preproc(frame, self.imgsz, self.mean, self.std)
            t1 = time.time()
            data = self.infer(blob)
            #fpsout = (fpsout + (1. / (time.time() - t1))) / 2
            #frame = cv2.putText(frame, "FPS:%d " %fpsout, (0, 40), cv2.FONT_HERSHEY_SIMPLEX, 1,
            #                    (0, 0, 255), 2)
            if end2end:
                num, final_boxes, final_scores, final_cls_inds = data
                final_boxes = np.reshape(final_boxes/ratio, (-1, 4))
                dets = np.concatenate([final_boxes[:num[0]], np.array(final_scores)[:num[0]].reshape(-1, 1), np.array(final_cls_inds)[:num[0]].reshape(-1, 1)], axis=-1)
            else:
                predictions = np.reshape(data, (1, -1, int(5+self.n_classes)))[0]
                dets = self.postprocess(predictions,ratio)

            dets = getwarningdets(dets, width, height)

            frame_idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES))

            if dets is not None and dets.size > 0:
                subtitles[f'{frame_idx}'] = {}
                final_boxes, final_scores, final_cls_inds, final_warn_inds = dets[:,:4], dets[:, 4], dets[:, 5], dets[:,6]
                frame = vis(frame, final_boxes, final_scores, final_cls_inds, final_warn_inds,
                                conf=conf, class_names=self.class_names_en)
                timestamp = cap.get(cv2.CAP_PROP_POS_MSEC)

                for i, [x1,y1,x2,y2,score,cls,warn] in enumerate(dets):
                    print(f'{file_name}: Frame:{frame_idx} ObjIndex:{i} Time:{timestamp:.2f}--Class:{int(cls)} Warning:{int(warn)}')
                    warning_name = self.warning_names[int(warn)]
                    location_name = self.location_names[int(((x1+x2)//2)//(width//3))]
                    subtitles[f'{frame_idx}'][f'{i}'] = {"class":self.class_names_kr[int(cls)], 
                            "location":f'{location_name}', "warning_lv":f'{warning_name}'} 

            #cv2.imshow('frame', frame)
            out.write(frame)
            if cv2.waitKey(25) & 0xFF == ord('q'):
                break
        out.release()
        cap.release()
        cv2.destroyAllWindows()
        outfilename = 'detecth264.mp4'
        h264_encoding_withgpu(outfilename_raw, outfilename)
        json_file_path = file_name[:-4]+'-subtitle.json' 
        with open(json_file_path,'w',encoding='UTF-8') as file:
            json.dump(subtitles,file,indent=4)
        json_obj = json.dumps(subtitles, ensure_ascii=False,indent=None,sort_keys=True)

        return outfilename, json_obj        

    def inference(self, img_path, conf=0.5, end2end=False):
        origin_img = cv2.imread(img_path)
        img, ratio = preproc(origin_img, self.imgsz, self.mean, self.std)
        data = self.infer(img)
        if end2end:
            num, final_boxes, final_scores, final_cls_inds = data
            final_boxes = np.reshape(final_boxes/ratio, (-1, 4))
            dets = np.concatenate([final_boxes[:num[0]], np.array(final_scores)[:num[0]].reshape(-1, 1), np.array(final_cls_inds)[:num[0]].reshape(-1, 1)], axis=-1)
        else:
            predictions = np.reshape(data, (1, -1, int(5+self.n_classes)))[0]
            dets = self.postprocess(predictions,ratio,width,height)

        if dets is not None:
            final_boxes, final_scores, final_cls_inds, final_warn_inds = dets[:,
                    :4], dets[:, 4], dets[:, 5], dets[:, 6]
            origin_img = vis(origin_img, final_boxes, final_scores, final_cls_inds, final_warn_indx,
                             conf=conf, class_names=self.class_names_en)
        return origin_img

    @staticmethod
    def postprocess(predictions, ratio):
        boxes = predictions[:, :4]
        scores = predictions[:, 4:5] * predictions[:, 5:]
        boxes_xyxy = np.ones_like(boxes)
        boxes_xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2.
        boxes_xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2.
        boxes_xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2.
        boxes_xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2.
        boxes_xyxy /= ratio
        dets = multiclass_nms(boxes_xyxy, scores, nms_thr=0.45, score_thr=0.1)
        return warningdets

    def get_fps(self):
        import time
        img = np.ones((1,3,self.imgsz[0], self.imgsz[1]))
        img = np.ascontiguousarray(img, dtype=np.float32)
        for _ in range(5):  # warmup
            _ = self.infer(img)

        t0 = time.perf_counter()
        for _ in range(100):  # calculate average time
            _ = self.infer(img)
        print(100/(time.perf_counter() - t0), 'FPS')


def nms(boxes, scores, nms_thr):
    """Single class NMS implemented in Numpy."""
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]

    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)

        inds = np.where(ovr <= nms_thr)[0]
        order = order[inds + 1]

    return keep


def multiclass_nms(boxes, scores, nms_thr, score_thr):
    """Multiclass NMS implemented in Numpy"""
    final_dets = []
    num_classes = scores.shape[1]
    for cls_ind in range(num_classes):
        cls_scores = scores[:, cls_ind]
        valid_score_mask = cls_scores > score_thr
        if valid_score_mask.sum() == 0:
            continue
        else:
            valid_scores = cls_scores[valid_score_mask]
            valid_boxes = boxes[valid_score_mask]
            keep = nms(valid_boxes, valid_scores, nms_thr)
            if len(keep) > 0:
                cls_inds = np.ones((len(keep), 1)) * cls_ind
                dets = np.concatenate(
                    [valid_boxes[keep], valid_scores[keep, None], cls_inds], 1
                )
                final_dets.append(dets)
    if len(final_dets) == 0:
        return None
    return np.concatenate(final_dets, 0)


def getwarningdets(dets,width,height):
    warningdets = []
    T1 = 0.7 #threshold1 y
    T2_x1 = 0.3 #threshold2 xleft
    T2_x2 =0.7 #threshold2 xright
    T2_y = 0.9 #threshold2 y

    for x1,y1,x2,y2,score,cls in dets.tolist():
        box=[x1, y1, x2, y2]

        if box[3]>height*T1: # y - close
            warn=2
            d_x, d_y = (box[0] + box[2]) / 2 - width / 2, box[3] - height
            dist = math.sqrt(pow(d_x,2)+pow(d_y,2))
            angle = 90 - math.atan2(-d_y,d_x)*180/math.pi
            
            if dist<height*0.1 or (height*0.1<dist<height*0.2 and -45<=angle<=45) or -15<=angle<=15:
            #if box[3]>height*T2_y and not(box[0]>width*T2_x2 or box[2]<width*T2_x1): # x - center
                warn=1

            warningdets.append([x1,y1,x2,y2,score,cls,warn])

    warningdets = np.array(warningdets)

    return warningdets


def preproc(image, input_size, mean, std, swap=(2, 0, 1)):
    if len(image.shape) == 3:
        padded_img = np.ones((input_size[0], input_size[1], 3)) * 114.0
    else:
        padded_img = np.ones(input_size) * 114.0
    img = np.array(image)
    r = min(input_size[0] / img.shape[0], input_size[1] / img.shape[1])
    resized_img = cv2.resize(
        img,
        (int(img.shape[1] * r), int(img.shape[0] * r)),
        interpolation=cv2.INTER_LINEAR,
    ).astype(np.float32)
    padded_img[: int(img.shape[0] * r), : int(img.shape[1] * r)] = resized_img
    # if use yolox set
    # padded_img = padded_img[:, :, ::-1]
    # padded_img /= 255.0
    padded_img = padded_img[:, :, ::-1]
    padded_img /= 255.0
    if mean is not None:
        padded_img -= mean
    if std is not None:
        padded_img /= std
    padded_img = padded_img.transpose(swap)
    padded_img = np.ascontiguousarray(padded_img, dtype=np.float32)
    return padded_img, r


def rainbow_fill(size=50):  # simpler way to generate rainbow color
    cmap = plt.get_cmap('jet')
    color_list = []

    for n in range(size):
        color = cmap(n/size)
        color_list.append(color[:3])  # might need rounding? (round(x, 3) for x in color)[:3]

    return np.array(color_list)


_COLORS = rainbow_fill(80).astype(np.float32).reshape(-1, 3)


def vis(img, boxes, scores, cls_ids, warn_idxs, conf=0.5, class_names=None):
    for i in range(len(boxes)):
        box = boxes[i]
        cls_id = int(cls_ids[i])
        warn_idx = int(warn_idxs[i])
        score = scores[i]
        if score < conf:
            continue
        x0 = int(box[0])
        y0 = int(box[1])
        x1 = int(box[2])
        y1 = int(box[3])

        color=colors(4*(warn_idx-1),True)
        #color = (_COLORS[cls_id] * 255).astype(np.uint8).tolist()
        text = '{}:{:.1f}%'.format(class_names[cls_id], score * 100)
        txt_color = (0, 0, 0) if np.mean(_COLORS[cls_id]) > 0.5 else (255, 255, 255)
        font = cv2.FONT_HERSHEY_SIMPLEX

        txt_size = cv2.getTextSize(text, font, 0.4, 1)[0]
        cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)

        #txt_bk_color = (_COLORS[cls_id] * 255 * 0.7).astype(np.uint8).tolist()
        cv2.rectangle(
            img,
            (x0, y0 + 1),
            (x0 + txt_size[0] + 1, y0 + int(1.5 * txt_size[1])),
            color,
            -1
        )
        cv2.putText(img, text, (x0, y0 + txt_size[1]), font, 0.4, txt_color, thickness=1)

    return img
