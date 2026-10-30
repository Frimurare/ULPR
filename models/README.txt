Detektorvikter placeras här (ingår inte i källkoden).

GPU (backend ultralytics): plates-v1m.pt eller plates-v1s.pt
  (morsetechlab/yolov11-license-plate-detection, AGPL-3.0).

Raspberry Pi + Hailo (backend hailo): en HEF kompilerad för rätt krets, t.ex. Hailo Model Zoo
  tiny_yolov4_license_plates för hailo10h:
  https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v5.0.0/hailo10h/tiny_yolov4_license_plates.hef
  Versionen ska matcha HailoRT på enheten (HailoRT 5.1 kör v5.0.0-HEF:er).
  Egna HEF:er (t.ex. plates-v1s kompilerad med Hailo Dataflow Compiler) fungerar om de har
  NMS på kretsen (HAILO NMS BY CLASS, klass 0 = skylt).

Inställningar (app.py installning NYCKEL VÄRDE): detector = sökväg till filen, det_backend
  (tomt = väljs av filändelsen), hailo_tile (rutstorlek i källpixlar, 832 för 5 Mpix),
  hailo_overlap (minst bredaste skylten i px), decoder = pyav för hårdvaruavkodning.
