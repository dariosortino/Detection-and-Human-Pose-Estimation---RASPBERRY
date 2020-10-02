#!/usr/bin/env python3

import argparse
import os
import sys
import cv2
import time
import numpy as np
from PIL import Image

from edgetpu.basic import edgetpu_utils
from pose_engine import PoseEngine
from imutils.video.pivideostream import PiVideoStream
from imutils.video.filevideostream import FileVideoStream
import imutils

from openvino.inference_engine import IECore

from detector import Detector
from estimator import HumanPoseEstimator

from picamera.array import PiRGBArray
from picamera import PiCamera
import logging
import sys

logging.basicConfig(format="[ %(levelname)s ] %(message)s",
                    level=logging.INFO,
                    stream=sys.stdout)
log = logging.getLogger()


EDGES = (
    ('nose', 'left eye'),
    ('nose', 'right eye'),
    ('nose', 'left ear'),
    ('nose', 'right ear'),
    ('left ear', 'left eye'),
    ('right ear', 'right eye'),
    ('left eye', 'right eye'),
    ('left shoulder', 'right shoulder'),
    ('left shoulder', 'left elbow'),
    ('left shoulder', 'left hip'),
    ('right shoulder', 'right elbow'),
    ('right shoulder', 'right hip'),
    ('left elbow', 'left wrist'),
    ('right elbow', 'right wrist'),
    ('left hip', 'right hip'),
    ('left hip', 'left knee'),
    ('right hip', 'right knee'),
    ('left knee', 'left ankle'),
    ('right knee', 'right ankle'),
)





def build_argparser():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m_od", "--model_od", type=str, default= "mobilenet-ssd.xml",
                        help="path to model of object detector to be infered in NCS2, in xml format")

    parser.add_argument("-m_hpe", "--model_hpe", default="models/posenet_mobilenet_v1_075_481_641_quant_decoder_edgetpu.tflite", type=str,
                            help="path to model of human pose estimator to be infered in Google Coral TPU, TFlite model. Assigned one by default")

    parser.add_argument("-i", "--input", type=str, nargs='+', default='0', help="path to video or image/images")
    parser.add_argument("-d", "--device", type=str, default='MYRIAD', required=False,
                        help="Specify the target to infer on CPU, GPU, or MYRIAD")
    parser.add_argument("--person_label", type=int, required=False, default=1, help="Label of class person for detector")
    parser.add_argument("--modality", type=str, default="Multi", help="Define the modality of representation of the output. Set single to visualize the skeleton of the main actor")
    parser.add_argument("--no_show", help='Optional. Do not display output.', action='store_true')
    
    return parser

class ImageReader(object):
    def __init__(self, file_names):
        self.file_names = file_names
        self.max_idx = len(file_names)

    def __iter__(self):
        self.idx = 0
        return self

    def __next__(self):
        if self.idx == self.max_idx:
            raise StopIteration
        img = cv2.imread(self.file_names[self.idx], cv2.IMREAD_COLOR)
        if img.size == 0:
            raise IOError('Image {} cannot be read'.format(self.file_names[self.idx]))
        self.idx += 1
        return img


class VideoReader(object):
    def __init__(self, file_name):
        try:
            self.file_name = int(file_name[0])
        except:
            self.file_name = file_name[0]


    def __iter__(self):
        self.cap = cv2.VideoCapture(self.file_name)
        if not self.cap.isOpened():
            raise IOError('Video {} cannot be opened'.format(self.file_name))
        return self

    def __next__(self):
        was_read, img = self.cap.read()
        if not was_read:
            raise StopIteration
        return img
        
def draw_pose(img, pose, person, mode,threshold=0.4):
    xys = {}
    if mode == "single":
        for label, keypoint in pose.keypoints.items():
            if keypoint.score < threshold: continue
            xys[label] = (int(keypoint.yx[1]), int(keypoint.yx[0]))
            if (keypoint.yx[1]>person[0] and keypoint.yx[1]<(person[0]+person[2])) and (keypoint.yx[0]>person[1] and keypoint.yx[0]<(person[1]+person[3])):
                img = cv2.circle(img, (int(keypoint.yx[1]), int(keypoint.yx[0])), 5, (0, 255, 0), -1)
                cv2.putText(img, label, (int(keypoint.yx[1])+3, int(keypoint.yx[0])-7), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255,255,255))
    else:
        for label, keypoint in pose.keypoints.items():
            if keypoint.score < threshold: continue
            xys[label] = (int(keypoint.yx[1]), int(keypoint.yx[0]))
            img = cv2.circle(img, (int(keypoint.yx[1]), int(keypoint.yx[0])), 5, (0, 255, 0), -1)
            cv2.putText(img, label, (int(keypoint.yx[1])+3, int(keypoint.yx[0])-7), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255,255,255))
        

    for a, b in EDGES:
        if a not in xys or b not in xys: continue
        ax, ay = xys[a]
        bx, by = xys[b]
        if mode == "single":
            if ax>person[0] and ax<(person[0]+person[2]) and ay>person[1] and ay<(person[1]+person[3]):
                img = cv2.line(img, (ax, ay), (bx, by), (255,0,0), 2)
        else:
            img = cv2.line(img, (ax, ay), (bx, by), (255,0,0), 2)


def overlay_on_image(frames, result, model_width, model_height,person,mode):

    color_image = frames

    if isinstance(result, type(None)):
        return color_image
    img_cp = color_image.copy()

    for pose in result:
        draw_pose(img_cp, pose, person,mode)

    return img_cp

def run_demo(args):
    detectfps = ""
    framecount = 0
    detectframecount = 0
    estimation_fps = ""
    detection_fps = ""
    fps = ""
    time1 = 0
    time2 = 0

    camera_width  = 640
    camera_height = 480
    model_width   = 640
    model_height  = 480
    
    if args.model_od == "mobilenet-ssd.xml":
        labels = ['Background','Person','Car', 'Bus', 'Bicycle','Motorcycle'] #???
    elif args.model_od == "ssdlite_mobilenet_v2.xml":
        labels = [" ",]
        file1 = open("labels.txt", 'r')
        while True:
            line = file1.readline().rstrip().split()
            if len(line) == 1: line = " "
            elif len(line) == 2: line = line[1]
            elif len(line) == 3: line = line[1] + " " + line[2]
            labels.append(line)
            if not line:
                labels.pop()
                break
        file1.close()
            
    
    devices = edgetpu_utils.ListEdgeTpuPaths(edgetpu_utils.EDGE_TPU_STATE_UNASSIGNED)
    engine = PoseEngine(args.model_hpe, devices[0])
    
    log.info("Device info:")
    print("{}{}".format(" "*8, args.device))
    ie = IECore()
    detector_person = Detector(ie, path_to_model_xml=args.model_od,
                              device=args.device,
                              label_class=args.person_label)
    
    print("DETECTOR  = ")
    print(detector_person.__dict__)
        
    if args.input != '':
        img = cv2.imread(args.input[0], cv2.IMREAD_COLOR)
        frames_reader, delay = (VideoReader(args.input), 1) if img is None else (ImageReader(args.input), 0)
    else:
        raise ValueError('--input has to be set')
    
    for frame in frames_reader:
        
        t1 = time.perf_counter()

        # Run inference.
        bboxes, labels_detected, score_detected, bboxes_person = detector_person.detect(frame)
        main_person = [0,0,0,0]
        areas=[]
        for bbox in bboxes_person:
            area = bbox[2]*bbox[3]
            areas.append(area)
        if areas:
            box_person_num = areas.index(max(areas))
            main_person = [bboxes_person[box_person_num][0],bboxes_person[box_person_num][1],bboxes_person[box_person_num][2],bboxes_person[box_person_num][3]]
            print(main_person)
            
        color_image = frame
        color_image = cv2.resize(color_image, (model_width, model_height))
        prepimg = color_image[:, :, ::-1].copy()         
        res, inference_time = engine.DetectPosesInImage(prepimg)
          
        print("LABELS DETECTED")
        print(labels_detected)
        print("SCORE DETECTED")
        print(score_detected)
        print("\n")

        colors = [(0, 0, 255),
                  (255, 0, 0), (0, 255, 0), (255, 0, 0), (0, 255, 0),
                  (255, 0, 0), (0, 255, 0), (255, 0, 0), (0, 255, 0),
                  (255, 0, 0), (0, 255, 0), (255, 0, 0), (0, 255, 0),
                  (255, 0, 0), (0, 255, 0), (255, 0, 0), (0, 255, 0)]
        if res:
            detectframecount += 1
            imdraw = overlay_on_image(color_image, res, model_width, model_height, main_person, args.modality)
            
        else:
            imdraw = color_image
        
        i=0
        for bbox in bboxes:
            cv2.rectangle(imdraw, (bbox[0], bbox[1]), (bbox[0] + bbox[2], bbox[1] + bbox[3]), (0, 0, 255), 1)
            #cv2.putText(imdraw, labels[args.person_label], (bbox[0]+3,bbox[1]-7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255,255,255))
            if len(labels_detected)>0:
                cv2.putText(imdraw, labels[labels_detected[i].astype(int)], (bbox[0]+3,bbox[1]-7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255,255,255))
                i+=1
            
        framecount += 1
        if framecount >= 15:
            fps       = "Overall: {:.1f} FPS, ".format(float(time1/15))
            estimation_fps = "Pose Est.: {:.1f} FPS, ".format(float(detectframecount/time2))
            framecount = 0
            detectframecount = 0
            time1 = 0
            time2 = 0
        t2 = time.perf_counter()
        elapsedTime = t2-t1
        time1 += 1/elapsedTime
        time2 += elapsedTime
        detection_fps = "Detection: {:.1f} FPS".format(float(1/detector_person.infer_time))
        display_fps = fps + estimation_fps + detection_fps

        cv2.putText(imdraw, display_fps, (5,20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1)

        if args.no_show:
            print(display_fps)
            continue
            
        cv2.imshow('Demo', imdraw)
        key = cv2.waitKey(delay)
        if key == 27:
            break

if __name__ == "__main__":
    args = build_argparser().parse_args()
    run_demo(args)
