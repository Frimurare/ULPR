"""Detektor-backends. Samma gränssnitt för alla: detect(frame_bgr, roi) -> [([x1, y1, x2, y2], conf), ...]
i originalbildens koordinater, samt times (deque med sekunder per bild) och gpu_mib().

  ultralytics  YOLO-vikter (.pt/.onnx/.engine) via Ultralytics, GPU om sådan finns.
  hailo        kompilerad HEF på Hailo-8/8L/10H via HailoRT (InferModel-API:t).
               Stora bilder delas upp i rutor (tiles) i full upplösning så att små skyltar
               behåller sina pixlar; rutorna körs i batch på kretsen.
"""
import collections
import logging
import threading
import time

import numpy as np

log = logging.getLogger('lpr')

BACKENDS = ('ultralytics', 'hailo')


def backend_for(settings):
    b = (settings.get('det_backend') or '').strip().lower()
    if b in BACKENDS:
        return b
    return 'hailo' if str(settings.get('detector', '')).lower().endswith('.hef') else 'ultralytics'


def create(settings):
    backend = backend_for(settings)
    if backend == 'hailo':
        return HailoDetector(settings['detector'], settings['det_conf'], tile=settings.get('hailo_tile', 0),
                             overlap=settings.get('hailo_overlap', 64))
    return UltralyticsDetector(settings['detector'], settings['det_imgsz'], settings['det_conf'])


def crop_roi(frame, roi):
    if not roi:
        return frame, 0, 0
    img = frame[roi[1]:roi[3], roi[0]:roi[2]]
    if img.size == 0:
        return frame, 0, 0
    return img, roi[0], roi[1]


def nms(boxes, scores, iou_thr):
    """Enkel NMS i numpy. boxes Nx4 (x1,y1,x2,y2). Returnerar index att behålla."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        # samma skylt från två överlappande rutor ger ofta en hel och en något mindre ruta: släng
        # även rutor som till största delen ligger inuti en starkare (andel av den mindre ytan)
        iomin = inter / (np.minimum(areas[i], areas[order[1:]]) + 1e-9)
        order = order[1:][(iou < iou_thr) & (iomin < 0.6)]
    return keep


# ---------------------------------------------------------------- Ultralytics (GPU/CPU)
class UltralyticsDetector:
    name = 'ultralytics'

    def __init__(self, path, imgsz, conf):
        import torch
        from ultralytics import YOLO
        self.torch = torch
        self.model = YOLO(path)
        self.imgsz, self.conf = imgsz, conf
        self.lock = threading.Lock()
        self.times = collections.deque(maxlen=500)

    def detect(self, frame, roi):
        img, ox, oy = crop_roi(frame, roi)
        t0 = time.perf_counter()
        with self.lock:
            r = self.model.predict(img, imgsz=self.imgsz, conf=self.conf, quantize=16, verbose=False)[0]
        self.times.append(time.perf_counter() - t0)
        return [([b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy], c)
                for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())]

    def gpu_mib(self):
        return self.torch.cuda.memory_reserved() / 2**20 if self.torch.cuda.is_available() else 0

    def info(self):
        return {'backend': self.name}


# ---------------------------------------------------------------- Hailo (HEF)
# Ankare för YOLOv4-tiny-huvuden (Hailo Model Zoo tiny_yolov4_license_plates), pixlar i nätverkets indata.
YOLO4_TINY_ANCHORS = {32: [(81, 82), (135, 169), (344, 319)], 16: [(10, 14), (23, 27), (37, 58)]}


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


class HailoDetector:
    """Kör en HEF på Hailo-kretsen. Stöder två utdataformer:
    * HAILO NMS BY CLASS (YOLOv5/8/11 kompilerade med NMS på kretsen) - klass 0 tolkas som skylt.
    * råa YOLOv4-tiny-huvuden (t.ex. Model Zoo tiny_yolov4_license_plates) - avkodas här.
    Bilden delas i kvadratiska rutor om `tile` px (0 = nätverkets egen storlek, dvs. ingen nedskalning)."""
    name = 'hailo'

    def __init__(self, path, conf, tile=0, overlap=64, iou=0.45, batch=8):
        from hailo_platform import FormatType, HailoSchedulingAlgorithm, VDevice
        self.conf, self.iou = conf, iou
        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)
        self.model = self.vdevice.create_infer_model(path)
        self.batch = batch
        self.model.set_batch_size(batch)
        self.inp = self.model.input()
        self.in_h, self.in_w = self.inp.shape[0], self.inp.shape[1]
        self.outputs = list(self.model.outputs)
        for o in self.outputs:
            o.set_format_type(FormatType.FLOAT32)
        self.nms_out = any('nms' in o.name.lower() for o in self.outputs)
        self.device_name = 'Hailo'
        self.phys = None
        try:
            self.phys = self.vdevice.get_physical_devices()[0]
            self.device_name = 'Hailo-' + str(self.phys.control.identify().device_architecture).split('.')[-1].replace('HAILO', '')
        except Exception:
            pass
        self.cfg_ctx = self.model.configure()
        self.configured = self.cfg_ctx.__enter__()
        self.bindings = []
        for _ in range(batch):
            outs = {o.name: np.empty(self.model.output(o.name).shape, dtype=np.float32) for o in self.outputs} \
                if not self.nms_out else None
            b = self.configured.create_bindings(output_buffers=outs) if outs else self.configured.create_bindings()
            self.bindings.append(b)
        self.tile = int(tile) or self.in_w
        self.overlap = int(overlap)
        self.lock = threading.Lock()
        self.times = collections.deque(maxlen=500)
        self.tiles_per_frame = 0
        log.info('Hailo: %s indata %dx%d, %s, ruta %d px, överlapp %d px, batch %d', path.rsplit('/', 1)[-1],
                 self.in_w, self.in_h, 'NMS på kretsen' if self.nms_out else 'YOLOv4-tiny-huvuden', self.tile,
                 self.overlap, batch)

    # -------- rutnät
    def _grid(self, W, H):
        t, step = self.tile, max(1, self.tile - self.overlap)

        def starts(n):
            if n <= t:
                return [0]
            s = list(range(0, n - t, step))
            s.append(n - t)
            return s
        return [(x, y) for y in starts(H) for x in starts(W)]

    def _prep(self, img):
        import cv2
        h, w = img.shape[:2]
        if (w, h) != (self.in_w, self.in_h):
            # rutor i bildkanten kan vara mindre än `tile`: fyll ut i stället för att sträcka
            s = min(self.in_w / max(w, self.tile), self.in_h / max(h, self.tile))
            nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
            canvas = np.zeros((self.in_h, self.in_w, 3), np.uint8)
            canvas[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA) if (nw, nh) != (w, h) else img
            return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB), s
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB), 1.0

    # -------- avkodning
    def _decode_yolo4(self, b):
        boxes, scores = [], []
        for o in self.outputs:
            out = b.output(o.name).get_buffer()
            gh, gw, ch = out.shape
            stride = self.in_w // gw
            anchors = YOLO4_TINY_ANCHORS.get(stride)
            if anchors is None:
                continue
            na = len(anchors)
            p = out.reshape(gh, gw, na, ch // na)
            obj = _sigmoid(p[..., 4]) * _sigmoid(p[..., 5:]).max(-1)
            ys, xs, a = np.nonzero(obj >= self.conf)
            if not len(ys):
                continue
            q = p[ys, xs, a]
            sxy = 1.05
            cx = (_sigmoid(q[:, 0]) * sxy - 0.5 * (sxy - 1) + xs) * stride
            cy = (_sigmoid(q[:, 1]) * sxy - 0.5 * (sxy - 1) + ys) * stride
            an = np.array(anchors, dtype=np.float32)[a]
            bw = np.exp(q[:, 2]) * an[:, 0]
            bh = np.exp(q[:, 3]) * an[:, 1]
            boxes.append(np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1))
            scores.append(obj[ys, xs, a])
        if not boxes:
            return np.zeros((0, 4), np.float32), np.zeros(0, np.float32)
        return np.concatenate(boxes), np.concatenate(scores)

    def _decode_nms(self, b):
        out = b.output(self.outputs[0].name).get_buffer()
        cls0 = out[0] if isinstance(out, (list, tuple)) else out
        arr = np.asarray(cls0, dtype=np.float32).reshape(-1, 5)
        arr = arr[arr[:, 4] >= self.conf]
        # HailoRT: [y1, x1, y2, x2, score], normaliserat 0..1
        bx = np.stack([arr[:, 1] * self.in_w, arr[:, 0] * self.in_h, arr[:, 3] * self.in_w, arr[:, 2] * self.in_h], 1)
        return bx, arr[:, 4]

    def _infer(self, batch_imgs):
        n = len(batch_imgs)
        for i, im in enumerate(batch_imgs):
            self.bindings[i].input().set_buffer(im)
        self.configured.run(self.bindings[:n], 5000)
        return [self._decode_nms(b) if self.nms_out else self._decode_yolo4(b) for b in self.bindings[:n]]

    def detect(self, frame, roi):
        img, ox, oy = crop_roi(frame, roi)
        H, W = img.shape[:2]
        t0 = time.perf_counter()
        grid = self._grid(W, H)
        self.tiles_per_frame = len(grid)
        all_b, all_s = [], []
        with self.lock:
            for i in range(0, len(grid), self.batch):
                chunk = grid[i:i + self.batch]
                prepped = [self._prep(img[y:y + self.tile, x:x + self.tile]) for x, y in chunk]
                res = self._infer([p[0] for p in prepped])
                for (x, y), (_, s), (bx, sc) in zip(chunk, prepped, res):
                    if len(bx):
                        bx = bx / s + [x, y, x, y]
                        # skyltar som skärs av en inre rutkant ligger hela i grannrutan (överlappet) - släng bitarna
                        e = 3
                        cut = ((bx[:, 0] <= x + e) & (x > 0)) | ((bx[:, 1] <= y + e) & (y > 0)) | \
                              ((bx[:, 2] >= x + self.tile - e) & (x + self.tile < W)) | \
                              ((bx[:, 3] >= y + self.tile - e) & (y + self.tile < H))
                        all_b.append(bx[~cut])
                        all_s.append(sc[~cut])
        out = []
        if all_b:
            boxes, scores = np.concatenate(all_b), np.concatenate(all_s)
            boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, W)
            boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, H)
            for k in nms(boxes, scores, self.iou):
                x1, y1, x2, y2 = boxes[k]
                if x2 - x1 >= 4 and y2 - y1 >= 2:
                    out.append(([float(x1 + ox), float(y1 + oy), float(x2 + ox), float(y2 + oy)], float(scores[k])))
        self.times.append(time.perf_counter() - t0)
        return out

    def gpu_mib(self):
        return 0

    def chip_temp(self):
        try:
            return round(float(self.phys.control.get_chip_temperature().ts0_temperature), 1)
        except Exception:
            return None

    def info(self):
        return {'backend': self.name, 'tiles': self.tiles_per_frame}
