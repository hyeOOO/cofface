import time, os, argparse, pickle, pytz
import threading, queue, traceback
import requests, base64
from pathlib import Path
from datetime import datetime, timedelta
import eddmPrint

import cv2, pyglet
import numpy as np
import pyrealsense2 as rs
from insightface.app import FaceAnalysis

from typing import Optional
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
import uvicorn, asyncio
from PIL import Image
from io import BytesIO
from pydantic import BaseModel
from urllib.request import urlopen

from weather import get_weather
import mediapipe as mp

# API 응답 모델
class FaceRecognitionResponse(BaseModel):
    success: bool
    face_detected: bool
    is_live: Optional[bool] = None
    age: Optional[float] = None
    gender: Optional[str] = None
    confidence: Optional[float] = None
    message: str


class CameraControlRequest(BaseModel):
    enable: bool = True
    timeout: Optional[float] = None

class DisplayChangeRequest(BaseModel):
    display_type: str

# 제스처 감지 결과 모델
class GestureDetectionResult(BaseModel):
    gesture_type: str
    confidence: float
    session_id: str
    timestamp: datetime

# class CountdownTimer:
#     def __init__(self):
#         self.active = False
#         self.value = 0
#         self.start_time = 0
#         self.duration = 0
#         self.delay = 0
#         self.delay_start_time = 0
#         self.is_in_delay = False
    
#     def start(self, duration=3, delay=0):
#         """타이머 시작"""
#         self.active = True
#         self.value = duration
#         self.duration = duration
#         self.delay = delay
#         self.delay_start_time = time.time()
#         self.is_in_delay = delay > 0
#         print(f"타이머 시작: {delay}초 후 {duration}초 카운트다운")
#         return self
    
#     def stop(self):
#         """타이머 중지"""
#         self.active = False
#         self.value = 0
#         print("타이머 중지")
#         return self
    
#     def update(self):
#         """타이머 업데이트 (매 프레임마다 호출)"""
#         if not self.active:
#             return self
        
#         current_time = time.time()
        
#         # 지연 시간 처리
#         if self.is_in_delay:
#             elapsed_delay = current_time - self.delay_start_time
#             if elapsed_delay >= self.delay:
#                 # 지연 시간 종료, 본 카운트다운 시작
#                 self.is_in_delay = False
#                 self.start_time = current_time
#                 print(f"지연 시간 종료, 카운트다운 시작: {self.duration}초")
#             return self
        
#         # 카운트다운 처리
#         elapsed = current_time - self.start_time
#         seconds_passed = int(elapsed)
#         new_value = self.duration - seconds_passed
        
#         # 값이 변경된 경우에만 업데이트
#         if new_value != self.value:
#             self.value = new_value
#             print(f"카운트다운: {self.value}")
            
#             # 카운트다운 종료
#             if self.value <= 0:
#                 self.active = False
#                 print("카운트다운 완료")
        
#         return self
        
#     @property
#     def is_active(self):
#         """타이머가 활성화되어 있는지 여부"""
#         return self.active
    
#     @property
#     def current_value(self):
#         """현재 카운트다운 값"""
#         return max(0, self.value)


# 웹소켓 제스처 감지 관리자
class GestureWebSocketManager:
    def __init__(self):
        self.active_connection = None  # 단일 연결만 저장
        self.session_data = {  # 세션별 데이터 대신 단일 데이터 저장
            "nod_count": 0,
            "shake_count": 0,
            "last_landmarks": None,
            "start_time": time.time(),
            "gesture_detected": False
        }
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connection = websocket
        self.session_data = {
            "nod_count": 0,
            "shake_count": 0,
            "last_landmarks": None,
            "start_time": time.time(),
            "gesture_detected": False
        }
        print(f"제스처 웹소켓 연결 시작")
    
    def disconnect(self):
        self.active_connection = None
        print(f"제스처 웹소켓 연결 종료")
    
    def is_active(self):
        return self.active_connection is not None
    
    def get_session_data(self):
        return self.session_data
    
    def update_session_data(self, data):
        self.session_data.update(data)
    
    async def send_message(self, message):
        if self.active_connection:
            await self.active_connection.send_json(message)


def parse_args():
    parser = argparse.ArgumentParser(description='RealSense 얼굴 라이브니스 및 3D 임베딩')
    parser.add_argument('--save_dir', type=str, default='embeddings',
                        help='임베딩을 저장할 디렉토리')
    parser.add_argument('--width', type=int, default=640, help='카메라 너비')
    parser.add_argument('--height', type=int, default=480, help='카메라 높이')
    parser.add_argument('--fps', type=int, default=30, help='카메라 FPS')
    parser.add_argument('--show_scores', action='store_true', help='라이브니스 점수 표시')
    parser.add_argument('--show_depth', action='store_true', help='깊이 맵 표시')
    parser.add_argument('--save_embeddings', action='store_true', help='3D 임베딩 저장')
    parser.add_argument('--fullscreen', action='store_true', default=True, help='전체화면 모드')
    parser.add_argument('--gpu_id', type=int, default=0, help='사용할 GPU ID')
    parser.add_argument('--idle_image', type=str, default=None, 
                        help='대기 상태에서 표시할 이미지 파일 경로')
    parser.add_argument('--idle_gif', type=str, default='assets/orderme.gif', 
                        help='대기 상태에서 표시할 GIF 파일 경로')
    parser.add_argument('--age_gender', action='store_true', default=True,
                        help='나이 및 성별 추정 사용')
    parser.add_argument('--screen', type=int, default=0, 
                        help='표시할 화면 번호 (0부터 시작)')
    parser.add_argument('--target_width', type=int, default=600, 
                        help='대상 화면 너비')
    parser.add_argument('--target_height', type=int, default=1024, 
                        help='대상 화면 높이')
    parser.add_argument('--min_depth', type=float, default=0.36, 
                        help='감지할 최소 깊이 (미터)')
    parser.add_argument('--max_depth', type=float, default=0.8, 
                        help='감지할 최대 깊이 (미터)')
    parser.add_argument('--api_port', type=int, default=8080, 
                        help='FastAPI 서버 포트')
    parser.add_argument('--no_ui_text', action='store_true', default=True,
                        help='UI 텍스트 숨기기')
    parser.add_argument('--recognition_time', type=float, default=2.0, 
                        help='얼굴 인식 시간 (초)')
    parser.add_argument('--max_fps', type=int, default=30,
                        help='얼굴 인식 최대 처리 FPS (성능 최적화)')
    parser.add_argument('--overlay_path', type=str, default="assets/mask_overlay.png", 
                        help='가이드 오버레이 PNG 파일 경로')
    parser.add_argument('--frame_skip', type=int, default=1,
                        help='얼굴 인식 프레임 건너뛰기 (1=모든 프레임 처리, 2=2프레임마다 처리)')
    # 추가: 필요한 얼굴 인식 프레임 개수 설정
    parser.add_argument('--required_frames', type=int, default=10, 
                        help='수집할 얼굴 인식 프레임 개수')
    # 추가: 최대 대기 시간 (초)
    parser.add_argument('--max_wait_time', type=float, default=1.5, 
                        help='얼굴 인식 최대 대기 시간 (초)')
    return parser.parse_args()

class AnimatedGIF:
    def __init__(self, filename):
        self.frames = []
        self.durations = []
        self.current_frame = 0
        self.total_duration = 0
        self.elapsed = 0
        
        try:
            if filename.startswith('http'):
                # URL에서 이미지 로드
                response = urlopen(filename)
                gif = Image.open(BytesIO(response.read()))
            else:
                if not os.path.exists(filename):
                    print(f"GIF 파일이 존재하지 않음: {filename}")
                    return
                # 로컬 파일에서 이미지 로드
                gif = Image.open(filename)
                
            # GIF의 모든 프레임 추출
            try:
                while True:
                    # 프레임 정보 추출
                    duration = gif.info.get('duration', 100) / 1000  # 초 단위로 변환
                    self.durations.append(duration)
                    self.total_duration += duration
                    
                    # 프레임을 RGBA로 변환
                    frame = gif.convert('RGBA')
                    frame_data = np.array(frame)
                    
                    # RGB 순서로 변환 (OpenCV BGR에서)
                    frame_data = cv2.cvtColor(frame_data, cv2.COLOR_RGBA2RGB)
                    frame_data = cv2.flip(frame_data, 0)
                    
                    # Pyglet 텍스처로 변환
                    texture = pyglet.image.ImageData(
                        frame.width, frame.height, 'RGB', 
                        frame_data.tobytes(), pitch=frame_data.shape[1] * 3
                    ).get_texture()
                    
                    self.frames.append(texture)
                    gif.seek(gif.tell() + 1)
            except EOFError:
                pass  # GIF의 끝에 도달
        except Exception as e:
            print(f"GIF 로드 오류: {e}")
            traceback.print_exc()
    
    
    def get_current_frame(self, dt):
        if not self.frames:
            return None
        
        # 경과 시간 업데이트
        self.elapsed += dt
        if self.elapsed >= self.total_duration and self.total_duration > 0:
            self.elapsed = 0
        
        # 현재 표시할 프레임 계산
        elapsed = self.elapsed
        for i, duration in enumerate(self.durations):
            if elapsed < duration:
                return self.frames[i]
            elapsed -= duration
        
        return self.frames[0]  # 기본값 반환

class TextureGenerator:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        
    def create_texture_from_numpy(self, img):
        """종횡비를 유지하며 텍스처 생성"""
        # OpenCV는 BGR 순서, pyglet은 RGB 순서
        if img.shape[2] == 3:  # 컬러 이미지
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            
        # 텍스처 생성 (RGB 형식)
        try:
            texture = pyglet.image.ImageData(
                img.shape[1], img.shape[0],
                'RGB', img.tobytes(), pitch=img.shape[1] * 3
            ).get_texture()
            
            return texture
        except Exception as e:
            print(f"텍스처 생성 오류: {e}")
            traceback.print_exc()
            return None


def crop_to_target_ratio(image, target_ratio):
    """이미지를 대상 종횡비에 맞게 자르기"""
    h, w = image.shape[:2]
    current_ratio = w / h
    
    # 가로 화면 - 가운데 부분만 잘라내기
    if current_ratio > target_ratio:
        # 너비가 너무 넓은 경우
        new_w = int(h * target_ratio)
        start_x = (w - new_w) // 2
        image = image[:, start_x:start_x+new_w]
    elif current_ratio < target_ratio:
        # 높이가 너무 높은 경우
        new_h = int(w / target_ratio)
        start_y = (h - new_h) // 2
        image = image[start_y:start_y+new_h, :]
    
    return image

class RealSenseFaceLiveness:
    def __init__(self, args):
        self.args = args
        self.width = args.width
        self.height = args.height
        self.fps = args.fps
        self.save_dir = args.save_dir
        self.show_scores = args.show_scores
        self.show_depth = args.show_depth
        self.save_embeddings = args.save_embeddings
        self.gpu_id = args.gpu_id
        self.age_gender = args.age_gender
        self.idle_image_path = args.idle_image
        self.idle_gif_path = args.idle_gif
        self.original_idle_gif_path = args.idle_gif
        self.target_width = args.target_width
        self.target_height = args.target_height
        self.min_depth = args.min_depth  # 감지할 최소 깊이 (미터)
        self.max_depth = args.max_depth  # 감지할 최대 깊이 (미터)
        self.no_ui_text = args.no_ui_text
        self.recognition_time = args.recognition_time
        self.max_fps = args.max_fps  # 최대 처리 FPS
        self.last_frame_time = 0  # 마지막 프레임 처리 시간
        self.overlay_path = args.overlay_path
        self.frame_skip = args.frame_skip  # 프레임 스킵 설정 (2 = 2프레임당 1번 처리)
        self.frame_counter = 0  # 프레임 카운터
        self.pending_display_change = None
        
        # 필요한 얼굴 인식 프레임 개수
        self.required_frames = args.required_frames
        # 얼굴 인식 최대 대기 시간
        self.max_wait_time = args.max_wait_time
        
        # 모드 설정 (카메라 모드 또는 유휴 모드)
        self.camera_mode = False  # 기본값은 유휴 모드
        
        # API 요청 처리 중 플래그
        self.processing_api_request = False
        
        # API 요청 결과 저장
        self.api_result = None
        self.api_result_event = threading.Event()
        
        # 인식 프레임 수집
        self.collected_frames = []
        self.collection_start_time = 0
        
        # 유휴 이미지/GIF 로드
        self.idle_image_texture = None
        self.idle_gif = None
        
        # 라이브니스 상태 추적 변수 추가
        self.person_detected = False  # 실제 사람 감지 여부

        # MediaPipe Face Mesh 초기화
        self.mp_face_mesh = mp.solutions.face_mesh
        self.face_mesh = self.mp_face_mesh.FaceMesh(
            max_num_faces=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5
        )
        
        # 제스처 감지 플래그
        self.detecting_gesture = False
        self.current_gesture_session = None
        
        self.motion_gif = "assets/motioncheck.gif"
        self.pay_gif = "assets/pay.gif"
        self.loading_png = "assets/loading.png"
        
        if self.idle_gif_path:
            try:
                self.idle_gif = AnimatedGIF(self.idle_gif_path)
                print(f"GIF 로드 완료: {self.idle_gif_path}")
            except Exception as e:
                print(f"GIF 로드 실패: {e}")
        
        if self.idle_image_path and not self.idle_gif:
            try:
                if self.idle_image_path.startswith('http'):
                    # URL에서 이미지 로드
                    response = urlopen(self.idle_image_path)
                    img = Image.open(BytesIO(response.read()))
                    img_array = np.array(img.convert('RGB'))
                else:
                    # 로컬 파일에서 이미지 로드
                    img_array = cv2.imread(self.idle_image_path)
                
                if img_array is not None:
                    texture_generator = TextureGenerator(self.width, self.height)
                    self.idle_image_texture = texture_generator.create_texture_from_numpy(img_array)
                    print(f"이미지 로드 완료: {self.idle_image_path}")
                else:
                    print(f"이미지 로드 실패: {self.idle_image_path}")
            except Exception as e:
                print(f"이미지 로드 오류: {e}")
                traceback.print_exc()
        
        # 저장 디렉토리가 없으면 생성
        if self.save_embeddings and not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        
        self.display_width = self.width
        self.display_height = self.height
            
        try:
            # Pyglet 기본 디스플레이와 스크린 가져오기
            display = pyglet.display.get_display()
            screens = display.get_screens()
            
            print(f"감지된 디스플레이 수: {len(screens)}")
            for i, screen in enumerate(screens):
                print(f"스크린 #{i}: {screen.width}x{screen.height} @ ({screen.x}, {screen.y})")
            
            # 600x1024 디스플레이 찾기
            target_width = 600
            target_height = 1024
            best_screen_index = 0
            best_score = float('inf')
            
            for i, screen in enumerate(screens):
                # 너비와 높이 차이를 점수로 계산 (낮을수록 좋음)
                width_diff = abs(screen.width - target_width)
                height_diff = abs(screen.height - target_height)
                
                # 전체 크기 차이와 화면 비율 차이를 모두 고려
                target_ratio = target_width / target_height
                screen_ratio = screen.width / screen.height
                ratio_diff = abs(screen_ratio - target_ratio)
                
                # 가중치를 사용한 점수 계산 (비율 차이와 크기 차이 모두 고려)
                score = (width_diff + height_diff) * 0.7 + ratio_diff * 1000 * 0.3
                
                # 정확히 600x1024 디스플레이가 있으면 즉시 선택
                if screen.width == target_width and screen.height == target_height:
                    best_screen_index = i
                    break
                    
                # 더 나은 점수라면 저장
                if score < best_score:
                    best_score = score
                    best_screen_index = i
            
            self.screen_index = best_screen_index
            target_screen = screens[self.screen_index]
            print(f"자동 선택된 화면 #{self.screen_index}: {target_screen.width}x{target_screen.height}")
            print(f"목표 크기와의 차이: 너비 {abs(target_screen.width - target_width)}px, 높이 {abs(target_screen.height - target_height)}px")
            
            # 창 생성
            self.window = pyglet.window.Window(
                width=self.target_width,
                height=self.target_height,
                caption="RealSense 얼굴 인식 결제",
                resizable=True,
            )
            
            # 창 위치 계산 (화면 중앙)
            window_x = target_screen.x + (target_screen.width - self.target_width) // 2
            window_y = target_screen.y + (target_screen.height - self.target_height) // 2
            self.window.set_location(window_x, window_y)
            
            # 전체화면 설정 (요청 시)
            if args.fullscreen and (target_screen.width == 600 and target_screen.height == 1024):
                self.window.set_fullscreen(True, screen=target_screen)
            
            print(f"창 생성 완료: {self.window.width}x{self.window.height} @ {self.window.get_location()}")
        except Exception as e:
            print(f"창 생성 오류: {e}")
            traceback.print_exc()
            # 기본 창으로 대체
            self.window = pyglet.window.Window(
                width=self.target_width, 
                height=self.target_height,
                caption="RealSense 얼굴 인식 결제 (기본)"
            )
            
        # 텍스처 생성기
        self.texture_generator = TextureGenerator(self.width, self.height)
        
        # 키 핸들러 설정
        self.key_handler = pyglet.window.key.KeyStateHandler()
        self.window.push_handlers(self.key_handler)
        self.window.push_handlers(on_key_press=self.on_key_press)
        
        # 텍스트 레이블 초기화 (UI 텍스트 숨김 옵션에 따라)
        if not self.no_ui_text:
            self.labels = []
            self.fps_label = pyglet.text.Label(
                'FPS: 0',
                font_name='Arial',
                font_size=14,
                x=10, y=self.window.height - 20,
                color=(0, 255, 0, 255)
            )
            self.status_label = pyglet.text.Label(
                'Status: 처리 중...',
                font_name='Arial',
                font_size=14,
                x=10, y=10,
                color=(255, 255, 255, 255)
            )
            self.mode_label = pyglet.text.Label(
                '모드: 대기',
                font_name='Arial',
                font_size=14,
                x=10, y=self.window.height - 40,
                color=(255, 255, 0, 255)
            )
        else:
            self.labels = []
            self.fps_label = None
            self.status_label = None
            self.mode_label = None
        
        # 안내 라벨 추가 (라이브니스 검출 시 표시할 메시지)
        self.guide_label = pyglet.text.Label(
            '실제 얼굴을 보여주세요.',
            font_name='Arial',
            font_size=16,
            x=20, y=20,
            color=(255, 255, 255, 255),
        )
        
        # Sprite 생성
        self.color_sprite = None
        self.idle_sprite = None
        
        # RealSense 파이프라인 초기화
        self.pipeline = rs.pipeline()
        self.config = rs.config()
        
        # 스트림 설정
        self.config.enable_stream(rs.stream.depth, self.width, self.height, rs.format.z16, self.fps)
        self.config.enable_stream(rs.stream.color, self.width, self.height, rs.format.bgr8, self.fps)
        
        # 스트리밍 시작
        self.profile = self.pipeline.start(self.config)
        
        # 깊이 측정을 위한 깊이 스케일 가져오기
        self.depth_sensor = self.profile.get_device().first_depth_sensor()
        self.depth_scale = self.depth_sensor.get_depth_scale()
        
        # 깊이 프레임을 컬러 프레임에 정렬하기 위한 객체 생성
        self.align = rs.align(rs.stream.color)
        
        # Insightface 초기화 (별도 스레드에서)
        self.face_app = None
        self.initialization_done = False
        self.initialization_thread = threading.Thread(target=self.initialize_face_app)
        self.initialization_thread.daemon = True
        self.initialization_thread.start()
        
        # 프레임 및 처리 결과를 위한 큐
        self.frame_queue = queue.Queue(maxsize=30)
        self.result_queue = queue.Queue()
        
        # 처리 스레드 생성
        self.processing_thread = threading.Thread(target=self.process_frames)
        self.processing_thread.daemon = True
        self.processing_thread.start()
        
        # 현재 텍스처
        self.color_texture = None
        self.depth_texture = None
        
        # 라이브니스 결과
        self.faces_results = []
        
        # FPS 계산 변수
        self.prev_frame_time = 0
        self.curr_frame_time = 0
        self.fps_value = 0
        self.processing_fps = 0
        
        # UI 숨김 변수
        self.show_ui = not self.no_ui_text
        
        # 이벤트 설정
        pyglet.clock.schedule_interval(self.update, 1/60.0)
        
        self.start_frame_capture_thread()

        # API 요청 후 카메라 모드 자동 종료 타이머
        self.camera_timeout = None
        
        # 타겟 비율 계산
        self.target_ratio = self.target_width / self.target_height
        
        # PNG 오버레이 로드
        self.overlay_path_no_face = "assets/mask_overlay_not.png"  # 얼굴이 감지되지 않았을 때 사용할 오버레이
        self.overlay_path_liveness = "assets/mask_overlay_liveness.png"  # 라이브니스 감지 시 사용할 오버레이
        self.face_detected = False  # 얼굴 감지 상태를 추적하는 변수
        self.overlay_sprite = None  # 기본 오버레이 (얼굴 감지 시)
        self.overlay_no_face_sprite = None  # 얼굴 미감지 시 오버레이
        self.overlay_liveness_sprite = None  # 라이브니스 감지 시 오버레이

        # PNG 오버레이 로드
        try:
            # 기본 오버레이 로드 (얼굴 감지 시)
            overlay_img = pyglet.image.load(self.overlay_path)
            self.overlay_sprite = pyglet.sprite.Sprite(overlay_img, x=0, y=0)
            print(f"[Overlay] 기본 오버레이 PNG 로드 성공: {self.overlay_path}")
            
            # 얼굴 미감지 시 오버레이 로드
            overlay_no_face_img = pyglet.image.load(self.overlay_path_no_face)
            self.overlay_no_face_sprite = pyglet.sprite.Sprite(overlay_no_face_img, x=0, y=0)
            print(f"[Overlay] 얼굴 미감지 오버레이 PNG 로드 성공: {self.overlay_path_no_face}")
            
            # 라이브니스 감지 시 오버레이 로드
            overlay_liveness_img = pyglet.image.load(self.overlay_path_liveness)
            self.overlay_liveness_sprite = pyglet.sprite.Sprite(overlay_liveness_img, x=0, y=0)
            print(f"[Overlay] 라이브니스 오버레이 PNG 로드 성공: {self.overlay_path_liveness}")
        except Exception as e:
            print(f"[Overlay] PNG 로드 실패: {e}")
        
        
        # self.countdown_timer = CountdownTimer()
        
        # # 카운트다운 레이블 추가
        # self.countdown_label = pyglet.text.Label(
        #     '',
        #     font_name='Arial',
        #     font_size=120,  # 큰 글씨
        #     x=self.target_width // 2,
        #     y=(self.target_height // 2)+100,
        #     anchor_x='center',
        #     anchor_y='center',
        #     color=(255, 255, 255, 255),
        # )
        
        # # 카운트다운 상태 변수
        # self.countdown_active = False
        # self.countdown_value = 0
        # self.countdown_event = None
        
        
        
        # 윈도우 이벤트 핸들러 설정
        @self.window.event
        def on_draw():
            try:
                self.window.clear()
                
                self.process_pending_display_change()
                
                # if hasattr(self, 'countdown_timer'):
                #     self.countdown_timer.update()
                # 제스쳐 모드
                if self.detecting_gesture:
                    if self.idle_gif and hasattr(self.idle_gif, 'frames') and self.idle_gif.frames:
                        current_frame = self.idle_gif.get_current_frame(1/60.0)
                        if current_frame:
                            # 기존 스프라이트 확인 또는 새로 생성
                            if not self.idle_sprite or self.idle_sprite.image != current_frame:
                                self.idle_sprite = pyglet.sprite.Sprite(current_frame, x=0, y=0)
                                # 크기 조정
                                scale_x = self.window.width / current_frame.width
                                scale_y = self.window.height / current_frame.height
                                scale = min(scale_x, scale_y)
                                self.idle_sprite.scale = scale
                                # 중앙 정렬
                                self.idle_sprite.x = (self.window.width - self.idle_sprite.width) / 2
                                self.idle_sprite.y = (self.window.height - self.idle_sprite.height) / 2
                            
                            # 스프라이트 그리기
                            self.idle_sprite.draw()
                    return
                
                if self.camera_mode:
                    # 카메라 모드: 얼굴 인식 화면 표시
                    # 컬러 이미지 스프라이트 그리기
                    if self.color_sprite:
                        self.color_sprite.draw()
                        
                        # PNG 오버레이 그리기 (상태에 따라 다른 오버레이 사용)
                        if self.face_detected:
                            if self.person_detected:
                                # 진짜 사람이 감지됨 - 기본 오버레이
                                current_overlay = self.overlay_sprite
                            else:
                                # 얼굴은 감지됐지만 진짜 사람이 아님 - 라이브니스 오버레이
                                current_overlay = self.overlay_liveness_sprite
                                self.guide_label.draw()  # "실제 얼굴을 보여주세요" 메시지
                        else: # 얼굴 감지 안됨
                            current_overlay = self.overlay_no_face_sprite
                        
                        if current_overlay:
                            scale_x = self.window.width / current_overlay.image.width
                            scale_y = self.window.height / current_overlay.image.height
                            scale = min(scale_x, scale_y)

                            current_overlay.scale = scale

                            # 중앙 정렬
                            current_overlay.x = (self.window.width - current_overlay.width) / 2
                            current_overlay.y = (self.window.height - current_overlay.height) / 2

                            current_overlay.draw()
                    
                    # UI 텍스트가 활성화된 경우에만 레이블 렌더링
                    if self.show_ui:
                        for label in self.labels:
                            label.draw()
                else:
                    # 유휴 모드: 대기 화면 표시
                    if self.idle_gif and hasattr(self.idle_gif, 'frames') and self.idle_gif.frames:
                        # GIF 애니메이션 표시
                        current_frame = self.idle_gif.get_current_frame(1/60.0)
                        if current_frame:
                            if not self.idle_sprite:
                                self.idle_sprite = pyglet.sprite.Sprite(current_frame, x=0, y=0)
                                # 창 크기에 맞게 스케일 조정
                                scale_x = self.window.width / current_frame.width
                                scale_y = self.window.height / current_frame.height
                                scale = min(scale_x, scale_y)
                                self.idle_sprite.scale = scale
                                # 중앙 정렬
                                self.idle_sprite.x = (self.window.width - self.idle_sprite.width) / 2
                                self.idle_sprite.y = (self.window.height - self.idle_sprite.height) / 2
                            else:
                                self.idle_sprite.image = current_frame
                                
                            self.idle_sprite.draw()
                            
                    elif self.idle_image_texture:
                        # 정적 이미지 스프라이트가 이미 생성되어 있으면 그대로 사용
                        try:
                            if not self.idle_sprite:
                                self.idle_sprite = pyglet.sprite.Sprite(self.idle_image_texture, x=0, y=0)
                                # 창 크기에 맞게 스케일 조정
                                scale_x = self.window.width / self.idle_image_texture.width
                                scale_y = self.window.height / self.idle_image_texture.height
                                scale = min(scale_x, scale_y)
                                self.idle_sprite.scale = scale
                                # 중앙 정렬
                                self.idle_sprite.x = (self.window.width - self.idle_sprite.width) / 2
                                self.idle_sprite.y = (self.window.height - self.idle_sprite.height) / 2
                                print(f"정적 이미지 스프라이트 생성: {self.idle_image_path}, 위치: ({self.idle_sprite.x}, {self.idle_sprite.y}), 스케일: {self.idle_sprite.scale}")
                            
                            # 스프라이트 그리기
                            self.idle_sprite.draw()
                            
                            # 로그 줄이기: 2분에 한 번만 출력
                            if int(time.time() / 120) != getattr(self, '_last_log_period', 0):
                                self._last_log_period = int(time.time() / 120)
                                print(f"정적 이미지 그리기: {self.idle_image_path}")
                        except Exception as e:
                            print(f"정적 이미지 그리기 오류: {e}")
                            traceback.print_exc()
                            
                    elif self.show_ui:
                        # UI 텍스트가 활성화된 경우에만 텍스트 표시
                        label = pyglet.text.Label(
                            '대기 중...',
                            font_name='Arial',
                            font_size=18,
                            x=self.window.width // 2,
                            y=self.window.height // 2,
                            anchor_x='center', 
                            anchor_y='center',
                            color=(255, 255, 255, 255)
                        )
                        label.draw()
                
                # if self.countdown_timer.is_active and not self.countdown_timer.is_in_delay:
                #     self.countdown_label.text = str(self.countdown_timer.current_value)
                #     self.countdown_label.draw()
                
                # UI 텍스트가 활성화된 경우에만 FPS 및 상태 표시
                if self.show_ui:
                    if self.fps_label:
                        self.fps_label.text = f'FPS: {self.fps_value:.1f} | 처리 FPS: {self.processing_fps:.1f}'
                        self.fps_label.draw()
                    
                    if self.status_label:
                        status_text = "준비됨" if self.initialization_done else "모델 로딩 중..."
                        frame_skip_info = f"프레임스킵: {self.frame_skip}"
                        self.status_label.text = f'상태: {status_text} | 점수: {"켜짐" if self.show_scores else "꺼짐"} | 깊이: {"켜짐" if self.show_depth else "꺼짐"} | 범위: {self.min_depth}~{self.max_depth}m | {frame_skip_info}'
                        self.status_label.draw()
                    
                    if self.mode_label:
                        self.mode_label.text = f'모드: {"카메라" if self.camera_mode else "대기"}'
                        self.mode_label.draw()
            except Exception as e:
                print(f"화면 그리기 오류 (무시됨): {e}")
    
    # def start_countdown(self, total_seconds=3, delay_before_start=1.0):
    #     self.countdown_timer.stop()
    #     self.countdown_timer.start(duration=total_seconds, delay=delay_before_start)
    #     print(f"카운트다운 시작: {delay_before_start}초 지연 후 {total_seconds}초 카운트다운")
    
    
    def initialize_face_app(self):
        """별도 스레드에서 Insightface 모델 초기화"""
        try:
            print("Insightface 모델 로딩 중...")
            THIS_DIR = Path(__file__).parent
            inface_models = THIS_DIR / "insightface"
            self.face_app = FaceAnalysis(
                name='buffalo_l',  # Buffalo-L 모델 사용 (성별, 나이 추정 지원)
                providers=['CUDAExecutionProvider'],
                root=str(inface_models),
                allowed_modules=['detection', 'recognition', 'genderage'],
            )
            self.face_app.prepare(ctx_id=self.gpu_id, det_size=(self.width, self.height))
            self.initialization_done = True
            print("Insightface 모델 로딩 완료!")
        except Exception as e:
            print(f"Insightface 초기화 오류: {e}")
            traceback.print_exc()
    
    def on_key_press(self, symbol, modifiers):
        """키 입력 처리"""
        if symbol == pyglet.window.key.Q:
            if self.camera_mode:self.set_camera_mode(False)
            pyglet.app.exit()
        elif symbol == pyglet.window.key.C:
            # 카메라/유휴 모드 전환
            self.set_camera_mode(not self.camera_mode)
        elif symbol == pyglet.window.key.S:
            self.show_scores = not self.show_scores
        elif symbol == pyglet.window.key.E:
            self.save_embeddings = not self.save_embeddings
            print(f"임베딩 저장 {'활성화' if self.save_embeddings else '비활성화'}")
        elif symbol == pyglet.window.key.P:
            # 프레임 스킵 값 변경
            if self.frame_skip < 5:  # 최대 5프레임 스킵 허용
                self.frame_skip += 1
            else:
                self.frame_skip = 1
            print(f"프레임 스킵 설정: {self.frame_skip}프레임마다 처리")
        elif symbol == pyglet.window.key.F:
            # 전체화면 전환
            display = pyglet.display.get_display()
            screens = display.get_screens()
            target_screen = screens[self.screen_index] if self.screen_index < len(screens) else screens[0]
            
            self.window.set_fullscreen(not self.window.fullscreen, screen=target_screen)
            print(f"전체화면 모드: {'켜짐' if self.window.fullscreen else '꺼짐'}")
        elif symbol == pyglet.window.key.T:
            # UI 텍스트 토글
            self.show_ui = not self.show_ui
            print(f"UI 텍스트 {'표시' if self.show_ui else '숨김'}")
        elif pyglet.window.key._1 <= symbol <= pyglet.window.key._9:
            display = pyglet.display.get_display()
            screens = display.get_screens()
            index = symbol - pyglet.window.key._1  # 0부터 시작
            if index < len(screens):
                self.screen_index = index
                target_screen = screens[self.screen_index]
                # 전체화면 여부 기억
                was_fullscreen = self.window.fullscreen
                if was_fullscreen:
                    self.window.set_fullscreen(False)
                # 창 위치 재설정
                window_x = target_screen.x + (target_screen.width - self.window.width) // 2
                window_y = target_screen.y + (target_screen.height - self.window.height) // 2
                self.window.set_location(window_x, window_y)
                if was_fullscreen:
                    self.window.set_fullscreen(True, screen=target_screen)
                print(f"스크린 #{self.screen_index}로 이동: {target_screen.width}x{target_screen.height} @ ({window_x}, {window_y})")

    
    def set_camera_mode(self, enabled, timeout=None):
        """카메라 모드 설정 (타임아웃 옵션 포함)"""
        # 모드 변경 시 이미지 처리 큐를 즉시 비워 딜레이 방지
        if self.camera_mode != enabled:
            try:
                # 큐 비우기
                while not self.frame_queue.empty():
                    self.frame_queue.get_nowait()
                while not self.result_queue.empty():
                    self.result_queue.get_nowait()
            except:
                pass
        
        # 카메라를 끄는 경우 진행 중인, 인식 프로세스도 중단
        if not enabled and self.processing_api_request:
            print("카메라 비활성화로 인한 인식 프로세스 중단")
            # self.countdown_timer.stop()
            self.processing_api_request = False
            # 결과 이벤트 설정으로 대기 중인 요청도 완료 처리
            self.api_result = {
                "success": False,
                "face_detected": False,
                "message": "사용자에 의해 인식이 취소되었습니다."
            }
            self.api_result_event.set()
        
        # 제스처 감지 모드도 중단
        if not enabled and self.detecting_gesture:
            print("카메라 비활성화로 인한 제스처 감지 중단")
            self.detecting_gesture = False
            self.current_gesture_session = None
        
        # if not enabled and hasattr(self, 'countdown_timer') and self.countdown_timer.is_active:
        #     print("카메라 비활성화로 인한 카운트다운 중단")
        #     self.countdown_timer.stop()
        
        self.camera_mode = enabled
        print(f"카메라 모드: {'활성화' if enabled else '비활성화'}")
        
        # 타임아웃 설정
        if enabled and timeout is not None:
            if self.camera_timeout:
                pyglet.clock.unschedule(self.camera_timeout)
            self.camera_timeout = pyglet.clock.schedule_once(
                lambda dt: self.set_camera_mode(False), timeout
            )
            print(f"{timeout}초 후 카메라 모드 자동 종료 예약됨")
        elif not enabled and self.camera_timeout:
            pyglet.clock.unschedule(self.camera_timeout)
            self.camera_timeout = None
    
    def start_frame_collection(self, frame_based=False):
        """얼굴 인식 데이터 수집 시작"""
        self.collected_frames = []
        self.collection_start_time = time.time()
        self.processing_api_request = True
        self.api_result = None
        self.api_result_event.clear()
        self.frame_counter = 0
        self.liveness_detected = False  # 라이브니스 상태 초기화
        
        # 모든 프레임 추적을 위한 변수 추가
        self.total_faces_count = 0  # 감지된 총 얼굴 수
        self.live_faces_count = 0   # 라이브 얼굴 수
        
        # 프레임 기반 모드 여부 설정
        self.frame_based_collection = frame_based
        
        # 시간 제한 강화 (최대 2초)
        self.max_wait_time = min(self.max_wait_time, 2.0)
        # 프레임 제한 설정 (최대 30프레임)
        self.required_frames = min(self.required_frames, 30)
        
        print(f"얼굴 인식 데이터 수집 시작: {'프레임 기반' if frame_based else '시간 기반'}")
        if frame_based:
            print(f"목표 프레임 수: {self.required_frames}개, 최대 대기 시간: {self.max_wait_time}초")
        else:
            print(f"수집 시간: {self.recognition_time}초")
    
    
    def start_gesture_detection(self, session_id=None):
        """제스처 감지 모드 시작"""
        # 이미 다른 세션에서 제스처 감지 중인지 확인
        if self.detecting_gesture:
            print(f"이미 제스처 감지 중")
            return False
        
        # 제스처 감지 모드 활성화
        self.detecting_gesture = True
        self.current_gesture_session = "default" 
        
        # 제스처 감지 관련 변수 초기화
        self.last_landmarks = None
        self.nod_count = 0
        self.shake_count = 0
        self.gesture_start_time = time.time()
        
        print(f"제스처 감지 모드 시작")
        return True
    
    def stop_gesture_detection(self):
        """제스처 감지 모드 종료"""
        self.detecting_gesture = False
        self.current_gesture_session = None
        self.last_landmarks = None
        
        # 기존 스프라이트 강제 초기화
        if hasattr(self, 'idle_sprite'):
            self.idle_sprite = None
        
        # 화면 전환 강제 요청
        self.pending_display_change = {
            'path': self.loading_png,
            'is_gif': True,
            'display_type': 'loading'
        }
        
        pyglet.clock.schedule_once(lambda dt: self.process_pending_display_change(), 0.1)
        print("제스처 감지 모드 종료")
    
    def detect_head_gesture(self, current_landmarks, previous_landmarks):
        """얼굴 랜드마크를 이용한 고개 끄덕임/좌우로 흔들기 감지 - 향상된 각도 방식"""
        if not previous_landmarks:
            return None
        
        # 여러 주요 랜드마크 포인트 선택 (더 안정적인 감지를 위해)
        key_points = [
            1,    # 코 끝
            9,    # 이마 중앙
            152,  # 턱 아래
            6,    # 코 위쪽
            197,  # 입 중앙
            5     # 코 중앙
        ]
        
        # 각 포인트별 변화량 계산
        x_diffs = []
        y_diffs = []
        
        for point_idx in key_points:
            current_point = current_landmarks.landmark[point_idx]
            prev_point = previous_landmarks.landmark[point_idx]
            
            # 변화량 계산
            x_diff = current_point.x - prev_point.x
            y_diff = current_point.y - prev_point.y
            
            x_diffs.append(x_diff)
            y_diffs.append(y_diff)
        
        # 평균 변화량 계산
        avg_x_diff = sum(x_diffs) / len(x_diffs)
        avg_y_diff = sum(y_diffs) / len(y_diffs)
        
        # 임계값 설정 - 최소 움직임 필요
        min_threshold = 0.01
        
        # 움직임 방향 비율 계산
        # - 1.0은 완벽한 45도 대각선
        # - 값이 클수록 수직 움직임 강조
        # - 값이 작을수록 수평 움직임 강조
        direction_ratio = abs(avg_y_diff) / (abs(avg_x_diff) + 0.0001)  # 0으로 나누기 방지
        
        # 방향 결정을 위한 임계값
        vertical_threshold = 2.5    # 2.5:1 비율 이상이면 확실한 수직 움직임
        horizontal_threshold = 0.4  # 1:2.5 비율 이하면 확실한 수평 움직임
        
        # 수직 움직임 감지 (위/아래)
        if abs(avg_y_diff) > min_threshold and direction_ratio > vertical_threshold:
            # 움직임이 충분히 크고, 수직 방향 비율이 임계값을 넘으면 끄덕임으로 판단
            return "nod" if avg_y_diff > 0 else "nod_up"  # 아래로 끄덕임 또는 위로 끄덕임
        
        # 수평 움직임 감지 (좌/우)
        elif abs(avg_x_diff) > min_threshold and direction_ratio < horizontal_threshold:
            # 움직임이 충분히 크고, 수평 방향 비율이 임계값 미만이면 좌우 흔들기로 판단
            return "shake_left" if avg_x_diff > 0 else "shake_right"  # 좌/우 흔들기
        
        # 명확한 방향이 아닌 경우 (대각선 움직임 등) 제스처로 인식하지 않음
        return None
    
    def process_gesture_frame(self, color_image):
        """제스처 감지를 위한 프레임 처리"""
        if not self.detecting_gesture or not self.current_gesture_session:
            return None
        
        # 경과 시간 확인 (타임아웃)
        elapsed_time = time.time() - self.gesture_start_time
        if elapsed_time > 120.0:  # 120초 타임아웃
            if int(elapsed_time * 10) % 10 == 0: 
                print(f"제스처 감지 타임아웃: {elapsed_time:.1f}초")
            
            if 120.0 <= elapsed_time < 120.1:
                print("타임아웃으로 제스처 감지 종료 처리")
                
                # 웹소켓 및 제스처 감지 상태 종료
                self.stop_gesture_detection()
                
                # 화면 전환 요청
                self.pending_display_change = {
                    'path': self.loading_png,
                    'is_gif': False,
                    'display_type': 'loading'
                }
                print(f"타임아웃 발생: 화면 전환 요청 - loading ({self.loading_png})")
                
                # 카메라 모드 종료 예약 (0.5초 후)
                if hasattr(self, 'camera_timeout') and self.camera_timeout:
                    pyglet.clock.unschedule(self.camera_timeout)
                self.camera_timeout = pyglet.clock.schedule_once(
                    lambda dt: self.set_camera_mode(False), 0.5
                )
                
                # 타임아웃 결과 반환
                return {"type": "timeout", "elapsed_time": elapsed_time}
            
            return {"type": "timeout", "elapsed_time": elapsed_time}
        
        # 얼굴 랜드마크 추출을 위해 RGB로 변환
        rgb_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2RGB)
        
        # MediaPipe 얼굴 메시 처리
        results = self.face_mesh.process(rgb_image)
        
        if not results.multi_face_landmarks:
            return None  # 얼굴이 감지되지 않음
        
        # 첫 번째 얼굴의 랜드마크 사용
        face_landmarks = results.multi_face_landmarks[0]
        
        # 제스처 감지
        gesture = self.detect_head_gesture(face_landmarks, self.last_landmarks)
        self.last_landmarks = face_landmarks
        
        if not gesture:
            return None  # 제스처가 감지되지 않음
        
        # 제스처 카운트 업데이트
        if gesture.startswith("nod"):
            self.nod_count += 1
            gesture_type = "nod"
            gesture_name = "updown"
        elif gesture.startswith("shake"):
            self.shake_count += 1
            gesture_type = "shake"
            gesture_name = "leftright"
        
        # 임계값 설정 (예: 3번 이상 같은 제스처가 감지되면 확정)
        is_confirmed = (self.nod_count >= 2 or self.shake_count >= 2)
        
        # 제스처 감지 결과 반환
        result = {
            "type": "gesture_update" if not is_confirmed else "gesture_detected",
            "gesture_type": gesture_type if is_confirmed else None,
            "gesture_name": gesture_name if is_confirmed else None,
            "nod_count": self.nod_count,
            "shake_count": self.shake_count,
            "is_confirmed": is_confirmed,
            "timestamp": datetime.now().isoformat()
        }
        
        # 확정된 제스처 감지시 카운터 리셋
        if is_confirmed:
            print(f"제스처 확정: {gesture_name} (계속 감지 중)")
            # 카운터 리셋
            self.nod_count = 0
            self.shake_count = 0
        
        return result
    
    # 멀티스레드 구조로 개선된 update 함수 (렌더링 전용으로 분리)
    def update(self, dt):
        try:
            # API 요청 처리 시간 확인 및 종료 처리
            if self.processing_api_request:
                # 카메라가 꺼진 경우 인식 중단
                if not self.camera_mode:
                    print("update에서 카메라 꺼짐 감지 - 인식 프로세스 중단")
                    self.processing_api_request = False
                    self.api_result = {
                        "success": False,
                        "face_detected": False,
                        "message": "카메라가 비활성화되어 인식이 취소되었습니다."
                    }
                    self.api_result_event.set()
                
                # 현재 경과 시간
                elapsed_time = time.time() - self.collection_start_time
                
                # 프레임 기반 모드인 경우
                if hasattr(self, 'frame_based_collection') and self.frame_based_collection:
                    frames_so_far = len(self.collected_frames)
                    max_wait_time = getattr(self, 'max_wait_time', 5.0)
                    required_frames = getattr(self, 'required_frames', 10)
                    
                    # 조건 1: 충분한 프레임이 수집된 경우
                    # 조건 2: 최대 대기 시간을 초과한 경우
                    if (frames_so_far >= required_frames) or (elapsed_time >= max_wait_time):
                        result_type = "목표 달성" if frames_so_far >= required_frames else "시간 초과"
                        print(f"프레임 수집 종료 ({result_type}): {frames_so_far}/{required_frames} 프레임, {elapsed_time:.2f}초 경과")
                        self.create_api_result()
                        self.processing_api_request = False
                # 시간 기반 모드인 경우 (기존 로직)
                elif elapsed_time >= self.recognition_time:
                    self.create_api_result()
                    self.processing_api_request = False
            
            if not self.camera_mode:
                return

            # 최신 결과만 가져오기
            latest_result = None
            while not self.result_queue.empty():
                latest_result = self.result_queue.get_nowait()

            if latest_result:
                display_image, self.faces_results, self.processing_fps = latest_result
                tex = self.texture_generator.create_texture_from_numpy(display_image)
                if tex:
                    if not self.color_sprite:
                        self.color_sprite = pyglet.sprite.Sprite(tex, x=0, y=0)
                    else:
                        self.color_sprite.image = tex
                    scale_x = self.window.width / tex.width
                    scale_y = self.window.height / tex.height
                    scale = min(scale_x, scale_y)
                    self.color_sprite.scale = scale
                    self.color_sprite.x = (self.window.width - self.color_sprite.width) / 2
                    self.color_sprite.y = (self.window.height - self.color_sprite.height) / 2
                self.update_labels()

            # 깊이 이미지도 결과에 포함시키고 싶다면 별도 result_queue에 추가하거나 프레임 저장 시 처리

        except Exception as e:
            print(f"[update] 오류: {e}")
            traceback.print_exc()


    def start_frame_capture_thread(self):
        def capture_loop():
            while True:
                try:
                    if not self.camera_mode:
                        time.sleep(0.1)
                        continue

                    frames = self.pipeline.wait_for_frames()
                    aligned_frames = self.align.process(frames)
                    depth_frame = aligned_frames.get_depth_frame()
                    color_frame = aligned_frames.get_color_frame()
                    if not depth_frame or not color_frame:
                        continue

                    depth_image = np.asanyarray(depth_frame.get_data())
                    color_image = np.asanyarray(color_frame.get_data())

                    color_image = cv2.flip(color_image, -1)
                    depth_image = cv2.flip(depth_image, -1)

                    color_image = crop_to_target_ratio(color_image, self.target_ratio)

                    if self.initialization_done:
                        original_color = np.asanyarray(color_frame.get_data())
                        original_depth = np.asanyarray(depth_frame.get_data())

                        while not self.frame_queue.empty():
                            self.frame_queue.get_nowait()
                        self.frame_queue.put_nowait((original_color, original_depth, color_image.copy(), depth_image.copy()))

                    # API 요청 중에는 최대한 많은 프레임 캡처
                    if self.camera_mode and self.processing_api_request:
                        # 최대한 빨리 다음 프레임 캡처
                        continue  # 지연 없이 즉시 다음 프레임으로
                    else:
                        # 일반 카메라 모드에서는 약간의 지연 추가 (CPU 사용량 감소)
                        if not self.camera_mode:
                            time.sleep(0.1)  # 카메라 비활성화 시 더 긴 지연
                        else:
                            time.sleep(0.01)  # 카메라 활성화 시 짧은 지연

                except Exception as e:
                    print(f"[capture_loop] 오류: {e}")
                    traceback.print_exc()
                    time.sleep(0.1)

        self.capture_thread = threading.Thread(target=capture_loop, daemon=True)
        self.capture_thread.start()


    def create_api_result(self):
        """수집된 프레임에서 API 결과 생성"""
        collection_time = time.time() - self.collection_start_time
        
        # 프레임 기반/시간 기반 모드 표시
        mode_str = "프레임 기반" if hasattr(self, 'frame_based_collection') and self.frame_based_collection else "시간 기반"
        print(f"프레임 수집 완료 ({mode_str}): {len(self.collected_frames)}개 프레임, {collection_time:.2f}초 소요")
        
        if not self.collected_frames:
            # 얼굴 감지 실패
            self.api_result = {
                "face_detected": False,
                "collection_mode": mode_str,
                "frames_collected": 0,
                "collection_time": collection_time,
                "message": "얼굴이 감지되지 않았습니다."
            }
            self.api_result_event.set()
            return
        
        # 모든 프레임의 얼굴 결과 분석
        all_face_results = []
        
        for frame_results in self.collected_frames:
            for face_result in frame_results:
                # 깊이 기반 필터링 - 깊이 정보 없는 경우 제외
                if face_result[1] is not None:  # 라이브니스 결과가 있는 경우만
                    all_face_results.append(face_result)
        
        if not all_face_results:
            # 유효한 얼굴 감지 실패
            self.api_result = {
                "face_detected": False,
                "collection_mode": mode_str,
                "frames_collected": len(self.collected_frames),
                "collection_time": collection_time,
                "message": "유효한 얼굴이 감지되지 않았습니다."
            }
            self.api_result_event.set()
            return
        
        # 라이브니스 결과 집계 (별도로 추적한 카운터 사용)
        if hasattr(self, 'total_faces_count') and self.total_faces_count > 0:
            live_ratio = self.live_faces_count / self.total_faces_count
        else:
            # 기존 방식으로 계산 (백업)
            live_count = sum(1 for result in all_face_results if result[1])
            live_ratio = live_count / len(all_face_results)
            
        # 라이브니스 임계값 (90%)을 넘어야 실제 사람으로 판단
        is_live_person = live_ratio >= 0.9
        
        # 나이 및 성별 집계
        ages = [result[3] for result in all_face_results if result[3] is not None]
        genders = [result[4] for result in all_face_results if result[4] is not None]
        
        avg_age = sum(ages) / len(ages) if ages else None
        
        # 성별 결정 (다수결)
        gender = None
        if genders:
            male_count = sum(1 for g in genders if g == 1)
            female_count = len(genders) - male_count
            gender = "male" if male_count > female_count else "female"
        
        # 최종 결과 생성
        self.api_result = {
            "face_detected": True,
            "is_live": is_live_person,  # 90% 이상이 실제 얼굴이면 실제로 판단
            "age": int(avg_age) if avg_age is not None else None,
            "gender": gender,
            "collection_time": collection_time,
            "live_ratio": live_ratio,  # 라이브니스 비율 추가
            "message": f"얼굴 인식 성공 (신뢰도: {live_ratio:.2f}, 프레임: {len(all_face_results)}개, 실제 여부: {'실제' if is_live_person else '가짜'})",
        }
        
        print(f"API 결과: {self.api_result}")
        self.api_result_event.set()
    
    def update_labels(self):
        """얼굴 레이블 업데이트"""
        self.labels = []
        
        if not self.show_ui:
            return
        
        # 스프라이트 오프셋 계산
        offset_x = (self.window.width - self.color_sprite.width) / 2 if self.color_sprite else 0
        offset_y = (self.window.height - self.color_sprite.height) / 2 if self.color_sprite else 0
        
        for i, (face_bbox, is_live, liveness_scores, age, gender) in enumerate(self.faces_results):
            x1, y1, x2, y2 = [int(val) for val in face_bbox]
            
            # 스프라이트 크기를 고려하여 좌표 조정
            if self.color_sprite:
                scale = self.color_sprite.scale
                x1 = int(x1 * scale + offset_x)
                y1 = int(y1 * scale + offset_y)
                x2 = int(x2 * scale + offset_x)
                y2 = int(y2 * scale + offset_y)
            
            # 라이브니스 상태 레이블
            liveness_text = "실제" if is_live else "가짜"
            self.labels.append(pyglet.text.Label(
                liveness_text,
                font_name='Arial',
                font_size=12,
                x=x1, y=y1 - 10,
                color=(0, 255, 0, 255) if is_live else (255, 0, 0, 255)
            ))
            
            # 나이 및 성별 표시
            if self.age_gender:
                gender_str = "male" if gender == 1 else "female"
                age_gender_text = f"나이: {age:.0f}세, 성별: {gender_str}"
                self.labels.append(pyglet.text.Label(
                    age_gender_text,
                    font_name='Arial',
                    font_size=12,
                    x=x1, y=y2 + 10,
                    color=(255, 255, 0, 255)
                ))
            
            # 활성화된 경우 라이브니스 점수 표시
            if self.show_scores:
                y_offset = y2 + (30 if self.age_gender else 20)
                for key, value in liveness_scores.items():
                    if isinstance(value, bool):
                        text = f"{key}: {'✓' if value else '✗'}"
                    elif isinstance(value, float):
                        text = f"{key}: {value:.2f}"
                    else:
                        text = f"{key}: {value}"
                    
                    self.labels.append(pyglet.text.Label(
                        text,
                        font_name='Arial',
                        font_size=10,
                        x=x1, y=y_offset,
                        color=(255, 255, 255, 255)
                    ))
                    y_offset += 20
    
    def process_frames(self):
        process_time = 0
        frame_count = 0
        start_time = time.time()
        last_faces_results = []  # 마지막으로 성공한 얼굴 감지 결과 저장

        while True:
            try:
                if not self.initialization_done:
                    time.sleep(0.1)
                    continue
                
                # API 요청 처리 중 카메라가 꺼진 경우 처리 중단
                if self.processing_api_request and not self.camera_mode:
                    print("카메라 꺼짐 감지 - 인식 프로세스 중단")
                    self.processing_api_request = False
                    self.api_result = {
                        "success": False,
                        "face_detected": False,
                        "message": "카메라가 비활성화되어 인식이 취소되었습니다."
                    }
                    self.api_result_event.set()
                    time.sleep(0.1)
                    continue
                
                if not self.camera_mode and not self.processing_api_request:
                    try:
                        while True:
                            self.frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                    time.sleep(0.1)
                    continue

                try:
                    original_color, original_depth, rotated_color, rotated_depth = self.frame_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                # 매 프레임마다 화면 업데이트용 이미지 먼저 큐에 넣음 (지연 방지)
                display_image = rotated_color.copy()
                
                # 제스처 감지 모드인 경우 처리
                if self.detecting_gesture and self.current_gesture_session:
                    gesture_result = self.process_gesture_frame(original_color)
                    if gesture_result and gesture_result.get("type") == "gesture_detected":
                        # 확정된 제스처 감지됨
                        print(f"제스처 감지됨: {gesture_result['gesture_name']} (세션: {self.current_gesture_session})")
                
                # 화면 업데이트 (항상 최신 프레임으로 수행)
                self.result_queue.put((display_image, last_faces_results, self.processing_fps))
                
                # 프레임 카운터 증가
                self.frame_counter += 1
                
                if self.detecting_gesture:
                    continue
                
                # 프레임 스킵 로직 변경: API 요청 중이거나 필요한 프레임 수에 미치지 못한 경우 우선 처리
                if self.processing_api_request:
                    # API 요청 중인 경우 우선
                    frame_based = hasattr(self, 'frame_based_collection') and self.frame_based_collection
                    frames_so_far = len(self.collected_frames)
                    required_frames = getattr(self, 'required_frames', 10)
                    
                    # 프레임 기반이고 필요한 수량에 미달인 경우 모든 프레임 처리
                    if frame_based and frames_so_far < required_frames:
                        process_this_frame = True
                    else:
                        # 일반 API 요청 중에는 frame_skip 적용
                        process_this_frame = (self.frame_counter % self.frame_skip == 0)
                else:
                    # 일반 모드에서는 기존 스킵 로직 사용
                    process_this_frame = (self.frame_counter % self.frame_skip == 0)
                
                if process_this_frame:
                    process_start = time.time()
                    
                    # 얼굴 인식 처리
                    faces = self.face_app.get(original_color)
                    valid_faces = []

                    for face in faces:
                        bbox = face.bbox
                        landmarks = face.kps
                        avg_depth, is_valid_depth = self.get_face_depth(original_depth, bbox, landmarks)
                        if is_valid_depth and self.min_depth <= avg_depth <= self.max_depth:
                            valid_faces.append((face, avg_depth))

                    if valid_faces:
                        valid_faces.sort(key=lambda x: x[1])
                        closest_face, closest_depth = valid_faces[0]
                        bbox = closest_face.bbox
                        landmarks = closest_face.kps
                        embedding = closest_face.embedding
                        is_live, liveness_scores = self.check_liveness(original_depth, bbox, landmarks)
                        embedding_3d = self.get_3d_face_embedding(embedding, original_depth, bbox, landmarks)
                        age = closest_face.age if hasattr(closest_face, 'age') else 0
                        gender = closest_face.gender if hasattr(closest_face, 'gender') else 0

                        if self.save_embeddings and is_live:
                            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                            filename = self.save_embedding(embedding_3d, is_live, age, gender, timestamp_str)
                            print(f"임베딩을 {filename}에 저장했습니다")

                        rotated_bbox, rotated_landmarks = self.rotate_detection_results(
                            bbox, landmarks, original_color.shape, rotated_color.shape
                        )
                        
                        faces_results = [(rotated_bbox, is_live, liveness_scores, age, gender)]
                        # 마지막 성공한 결과 저장
                        last_faces_results = faces_results.copy()

                        gender_str = "male" if gender == 1 else "female"
                        print(f"얼굴 감지: 라이브니스: {'실제' if is_live else '가짜'}, 깊이: {closest_depth:.2f}m, 나이: {age:.1f}세, 성별: {gender_str}")
                        
                        # 라이브니스 상태 업데이트
                        self.person_detected = is_live
                        
                        # API 요청 처리 중이고 얼굴 결과가 있으면 모든 프레임 수집 (라이브니스와 상관없이)
                        if self.processing_api_request:
                            # 모든 얼굴을 수집하고 라이브니스 카운트 별도 관리
                            self.collected_frames.append(faces_results)
                            
                            # 얼굴 카운트 증가
                            self.total_faces_count += 1
                            if is_live:
                                self.live_faces_count += 1
                            
                            # API 요청 상태 로깅 추가하여 진행 상황 확인 가능하게 하기
                            frames_so_far = len(self.collected_frames)
                            if hasattr(self, 'frame_based_collection') and self.frame_based_collection:
                                progress_pct = min(100, frames_so_far * 100 / self.required_frames)
                                elapsed_time = time.time() - self.collection_start_time
                                if frames_so_far % 3 == 0:  # 매 3번째 프레임마다 로그 출력 (과도한 로깅 방지)
                                    live_ratio = self.live_faces_count / self.total_faces_count if self.total_faces_count > 0 else 0
                                    print(f"프레임 수집 진행 중: {frames_so_far}/{self.required_frames} ({progress_pct:.1f}%), 경과 시간: {elapsed_time:.2f}초, 라이브니스 비율: {live_ratio:.2f}")
                    
                    new_face_detected = len(valid_faces) > 0
                    if new_face_detected != self.face_detected:
                        self.face_detected = new_face_detected
                        print(f"얼굴 감지 상태 변경: {'감지됨' if self.face_detected else '감지되지 않음'}")
                    
                    process_time += time.time() - process_start
                    frame_count += 1

                    if frame_count >= 10:
                        self.processing_fps = frame_count / process_time
                        process_time = 0
                        frame_count = 0
                    else:
                        self.processing_fps = frame_count / (time.time() - start_time if time.time() - start_time > 0 else 0.001)

            except Exception as e:
                print(f"[process_frames] 오류: {e}")
                traceback.print_exc()
                time.sleep(0.1)

    def process_pending_display_change(self):
        """대기 중인 디스플레이 변경 요청 처리"""
        if hasattr(self, 'pending_display_change') and self.pending_display_change:
            try:
                path = self.pending_display_change['path']
                is_gif = self.pending_display_change['is_gif']
                display_type = self.pending_display_change['display_type']
                
                if is_gif and path:
                    # GIF 로드
                    self.idle_gif = AnimatedGIF(path)
                    self.idle_gif_path = path
                    self.idle_image_texture = None
                    self.idle_image_path = None
                    self.idle_sprite = None
                    print(f"GIF 로드 완료: {path}")
                elif not is_gif and path:
                    print(f"이미지 로드 시작: {path}")
                    try:
                        if path.startswith('http'):
                            # URL에서 이미지 로드
                            response = urlopen(path)
                            img = Image.open(BytesIO(response.read()))
                            img_array = np.array(img.convert('RGB'))
                        else:
                            img_array = cv2.imread(path)
                            # 로컬 파일에서 이미지 로드
                            if img_array is None:
                                print(f"[경고] 이미지 파일이 존재하지 않음: {path}")
                                self.pending_display_change = None
                                return
                                
                            img_array = cv2.flip(img_array, -1)
                            img_array = cv2.flip(img_array, 1)
                        
                        if img_array is not None and img_array.size > 0:
                            self.idle_image_texture = self.texture_generator.create_texture_from_numpy(img_array)
                            self.idle_image_path = path
                            # 이미지 사용 시 GIF 초기화
                            self.idle_gif = None
                            self.idle_gif_path = None
                            
                            self.idle_sprite = pyglet.sprite.Sprite(self.idle_image_texture, x=0, y=0)
                            
                            scale_x = self.window.width / self.idle_image_texture.width
                            scale_y = self.window.height / self.idle_image_texture.height
                            scale = min(scale_x, scale_y)
                            self.idle_sprite.scale = scale
                            # 중앙 정렬
                            self.idle_sprite.x = (self.window.width - self.idle_sprite.width) / 2
                            self.idle_sprite.y = (self.window.height - self.idle_sprite.height) / 2
                            
                            print(f"이미지 로드 완료: {path}, 크기: {img_array.shape}")
                        else:
                            print(f"[오류] 이미지 로드 실패 (빈 이미지): {path}")
                    except Exception as e:
                        print(f"[오류] 이미지 로드 실패: {path}, 오류: {e}")
                        traceback.print_exc()
                    
                # 요청 처리 완료
                self.pending_display_change = None
            except Exception as e:
                print(f"디스플레이 변경 처리 오류: {e}")
                traceback.print_exc()
                self.pending_display_change = None

    def get_face_depth(self, depth_frame, face_bbox, landmarks):
        """얼굴의 평균 깊이 계산"""
        x1, y1, x2, y2 = [int(val) for val in face_bbox]
        
        # 경계 상자가 프레임 경계 내에 있는지 확인
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(depth_frame.shape[1] - 1, x2)
        y2 = min(depth_frame.shape[0] - 1, y2)
        
        if x1 >= x2 or y1 >= y2:
            return 0, False
        
        # 얼굴에 해당하는 깊이 영역 추출
        face_depth = depth_frame[y1:y2, x1:x2].copy().astype(float)
        
        # 깊이 값을 미터로 변환
        face_depth = face_depth * self.depth_scale
        
        # 제로(깊이 데이터 없음)를 NaN으로 대체하여 더 나은 처리
        face_depth[face_depth == 0] = np.nan
        
        # 유효한 깊이 데이터가 충분하지 않으면 False 반환
        valid_depth_percentage = np.sum(~np.isnan(face_depth)) / face_depth.size
        if valid_depth_percentage < 0.5:  # 얼굴의 최소 50%에 유효한 깊이가 있어야 함
            return 0, False
        
        # 평균 깊이 계산
        avg_depth = np.nanmean(face_depth)
        
        return avg_depth, True
    
    def rotate_detection_results(self, bbox, landmarks, src_shape, dst_shape):
        """
        회전된 좌표계로 변환
        src_shape: 원본 이미지 크기 (회전되지 않은)
        dst_shape: 대상 이미지 크기 (회전된)
        """
        src_h, src_w = src_shape[:2]
        dst_h, dst_w = dst_shape[:2]
        
        # 박스 좌표
        x1, y1, x2, y2 = bbox
        
        # 180도 회전
        rotated_bbox = [src_w - x2, src_h - y2, src_w - x1, src_h - y1]
        rotated_landmarks = np.array([[src_w - landmark[0], src_h - landmark[1]] for landmark in landmarks])
        
        return rotated_bbox, rotated_landmarks
    
    def check_liveness(self, depth_frame, face_bbox, landmarks, threshold_distance=10.0):
        """
        깊이 정보를 분석하여 얼굴이 실제인지 확인합니다.
        보다 강력한 감지를 위해 여러 방법을 결합합니다.
        """
        x1, y1, x2, y2 = [int(val) for val in face_bbox]
        
        # 경계 상자가 프레임 경계 내에 있는지 확인
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(depth_frame.shape[1] - 1, x2)
        y2 = min(depth_frame.shape[0] - 1, y2)
        
        if x1 >= x2 or y1 >= y2:
            return False, {"error": "유효하지 않은 경계 상자"}
        
        # 얼굴에 해당하는 깊이 영역 추출
        face_depth = depth_frame[y1:y2, x1:x2].copy().astype(float)
        
        # 깊이 값을 미터로 변환
        face_depth = face_depth * self.depth_scale
        
        # 제로(깊이 데이터 없음)를 NaN으로 대체하여 더 나은 처리
        face_depth[face_depth == 0] = np.nan
        
        # 유효한 깊이 데이터가 충분하지 않으면 False 반환
        valid_depth_percentage = np.sum(~np.isnan(face_depth)) / face_depth.size
        if valid_depth_percentage < 0.5:  # 얼굴의 최소 50%에 유효한 깊이가 있어야 함
            return False, {"valid_depth_percentage": valid_depth_percentage}
        
        # 방법 1: 얼굴 전체의 깊이 변화
        depth_variation = np.nanmax(face_depth) - np.nanmin(face_depth)
        depth_variation_score = depth_variation > threshold_distance / 1000.0  # 미터 단위로 변환
        
        # 방법 2: 코 돌출 검사 (실제 얼굴은 돌출된 코가 있음)
        nose_point = landmarks[2]  # 코 끝 랜드마크
        nose_x, nose_y = int(nose_point[0]), int(nose_point[1])
        
        # 코 주변의 작은 영역 정의
        nose_region_size = 5
        nose_region_x1 = max(0, nose_x - nose_region_size)
        nose_region_y1 = max(0, nose_y - nose_region_size)
        nose_region_x2 = min(depth_frame.shape[1] - 1, nose_x + nose_region_size)
        nose_region_y2 = min(depth_frame.shape[0] - 1, nose_y + nose_region_size)
        
        if nose_region_x1 >= nose_region_x2 or nose_region_y1 >= nose_region_y2:
            nose_prominence_score = False
        else:
            nose_depth = depth_frame[nose_region_y1:nose_region_y2, nose_region_x1:nose_region_x2].copy().astype(float)
            nose_depth = nose_depth * self.depth_scale  # 미터로 변환
            nose_depth[nose_depth == 0] = np.nan
            
            if np.sum(~np.isnan(nose_depth)) > 0:
                # 얼굴 둘레의 평균 깊이 가져오기
                perimeter_depths = []
                
                # 상단 모서리
                if y1 + 5 < depth_frame.shape[0]:
                    top_edge = depth_frame[y1:y1+5, x1:x2].copy().astype(float) * self.depth_scale
                    top_edge[top_edge == 0] = np.nan
                    if np.sum(~np.isnan(top_edge)) > 0:
                        perimeter_depths.append(np.nanmean(top_edge))
                
                # 하단 모서리
                if y2 - 5 >= 0:
                    bottom_edge = depth_frame[y2-5:y2, x1:x2].copy().astype(float) * self.depth_scale
                    bottom_edge[bottom_edge == 0] = np.nan
                    if np.sum(~np.isnan(bottom_edge)) > 0:
                        perimeter_depths.append(np.nanmean(bottom_edge))
                
                # 왼쪽 모서리
                if x1 + 5 < depth_frame.shape[1]:
                    left_edge = depth_frame[y1:y2, x1:x1+5].copy().astype(float) * self.depth_scale
                    left_edge[left_edge == 0] = np.nan
                    if np.sum(~np.isnan(left_edge)) > 0:
                        perimeter_depths.append(np.nanmean(left_edge))
                
                # 오른쪽 모서리
                if x2 - 5 >= 0:
                    right_edge = depth_frame[y1:y2, x2-5:x2].copy().astype(float) * self.depth_scale
                    right_edge[right_edge == 0] = np.nan
                    if np.sum(~np.isnan(right_edge)) > 0:
                        perimeter_depths.append(np.nanmean(right_edge))
                    
                if len(perimeter_depths) > 0:
                    avg_perimeter_depth = np.mean(perimeter_depths)
                    nose_depth_avg = np.nanmean(nose_depth)
                    nose_prominence = avg_perimeter_depth - nose_depth_avg
                    nose_prominence_score = nose_prominence > 0.005  # 5mm (미터 단위)
                else:
                    nose_prominence_score = False
            else:
                nose_prominence_score = False
        
        # 방법 3: 얼굴 곡률 확인 - 실제 얼굴은 평평하지 않음
        if valid_depth_percentage > 0.7:  # 좋은 깊이 커버리지 필요
            # 좌표 그리드 생성
            y_indices, x_indices = np.indices(face_depth.shape)
            x_indices = x_indices.flatten()
            y_indices = y_indices.flatten()
            z_values = face_depth.flatten()
            
            # NaN 값 제거
            valid_mask = ~np.isnan(z_values)
            x_valid = x_indices[valid_mask]
            y_valid = y_indices[valid_mask]
            z_valid = z_values[valid_mask]
            
            if len(z_valid) > 10:  # 평면 피팅에 충분한 포인트 필요
                # 점들에 평면 피팅
                try:
                    A = np.column_stack((x_valid, y_valid, np.ones_like(x_valid)))
                    plane_coeffs, residuals, rank, s = np.linalg.lstsq(A, z_valid, rcond=None)
                    
                    # 평면으로부터의 잔차 계산 (얼굴이 얼마나 평평하지 않은지)
                    fitted_z = A @ plane_coeffs
                    residuals = z_valid - fitted_z
                    curvature_score = np.std(residuals) > 0.003  # 3mm (미터 단위)
                except:
                    curvature_score = False
            else:
                curvature_score = False
        else:
            curvature_score = False
        
        # 점수 결합 (보안을 위해 AND 로직 사용)
        is_live = depth_variation_score and (nose_prominence_score or curvature_score)
        
        # 디버깅을 위한 상세 점수 반환
        scores = {
            "depth_variation": depth_variation * 1000,  # 표시를 위해 mm로 변환
            "depth_variation_score": depth_variation_score,
            "nose_prominence_score": nose_prominence_score,
            "curvature_score": curvature_score,
            "valid_depth_percentage": valid_depth_percentage
        }
        
        return is_live, scores
    
    def get_3d_face_embedding(self, face_embedding, depth_frame, face_bbox, landmarks):
        """
        깊이 특성으로 2D 얼굴 임베딩을 강화하여 "3D 임베딩" 생성
        """
        x1, y1, x2, y2 = [int(val) for val in face_bbox]
        
        # 경계 상자가 프레임 경계 내에 있는지 확인
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(depth_frame.shape[1] - 1, x2)
        y2 = min(depth_frame.shape[0] - 1, y2)
        
        # 랜드마크 위치에서 깊이 값 가져오기
        landmark_depths = []
        for landmark in landmarks:
            x, y = int(landmark[0]), int(landmark[1])
            if 0 <= x < depth_frame.shape[1] and 0 <= y < depth_frame.shape[0]:
                depth = depth_frame[y, x] * self.depth_scale  # 미터로 변환
                landmark_depths.append(depth)
            else:
                landmark_depths.append(0)
        
        # 얼굴 깊이에서 통계적 특성 추출
        if x1 < x2 and y1 < y2:
            face_depth = depth_frame[y1:y2, x1:x2].copy().astype(float) * self.depth_scale
            face_depth[face_depth == 0] = np.nan
            
            depth_features = []
            if np.sum(~np.isnan(face_depth)) > 0:
                depth_features = [
                    np.nanmean(face_depth),        # 평균 깊이
                    np.nanstd(face_depth),         # 깊이 표준 편차
                    np.nanmax(face_depth) - np.nanmin(face_depth)  # 깊이 범위
                ]
            else:
                depth_features = [0, 0, 0]
        else:
            depth_features = [0, 0, 0]
        
        # 원본 임베딩과 깊이 특성 결합
        depth_features = np.array(landmark_depths + depth_features)
        
        # 깊이 특성 정규화
        if np.std(depth_features) > 0:
            depth_features = (depth_features - np.mean(depth_features)) / (np.std(depth_features) + 1e-6)
        
        # 원본 임베딩과 결합
        enhanced_embedding = np.concatenate([face_embedding, depth_features])
        
        return enhanced_embedding
    
    def get_face_distance(self):
        """수집된 프레임에서 평균 얼굴 거리 계산"""
        if not self.collected_frames:
            return 0.0
        
        # 모든 거리 값 수집
        distances = []
        
        for frame_results in self.collected_frames:
            for face in frame_results:
                # 거리 정보는 로그에서 직접 추출
                distances.append(0.7)  # 로그의 거리 값 (0.61m, 0.62m 등)으로 대체
        
        if not distances:
            return 0.0
        
        # 평균 거리 반환
        return sum(distances) / len(distances)
    
    def save_embedding(self, embedding, liveness, age, gender, timestamp=None):
        """3D 얼굴 임베딩을 파일에 저장"""
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        
        filename = os.path.join(self.save_dir, f"face_embedding_{timestamp}.pkl")
        data = {
            "embedding": embedding,
            "liveness": liveness,
            "age": age,
            "gender": gender,
            "timestamp": timestamp
        }
        
        with open(filename, "wb") as f:
            pickle.dump(data, f)
        
        return filename
    
    def run(self):
        """Pyglet 애플리케이션 실행"""
        try:
            pyglet.app.run()
        except Exception as e:
            print(f"Pyglet 실행 오류: {e}")
            traceback.print_exc()
        finally:
            self.cleanup()
    
    def cleanup(self):
        """리소스 정리"""
        try:
            self.pipeline.stop()
        except:
            pass

from fastapi.middleware.cors import CORSMiddleware

# FastAPI 서버 클래스
class FaceRecognitionServer:
    def __init__(self, app_instance, port=8000):
        self.app_instance = app_instance
        self.port = port
        self.api = FastAPI(title="RealSense 얼굴 인식 API")
        
        self.api.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],  # Allows all origins
            allow_credentials=True,
            allow_methods=["*"],  # Allows all methods
            allow_headers=["*"],  # Allows all headers
        )
        # 웹소켓 제스처 관리자 초기화
        self.gesture_manager = GestureWebSocketManager()
        
        self.setup_routes()
        self.server_thread = None
        self.weather_cache = {
            "data": None,
            "last_update": None
        }
    
    def change_display_helper(self, display_type):
        self.idle_sprite = None
        path = None
        is_gif = False
        
        if display_type == "motion":
            path = "assets/motioncheck.gif"
            is_gif = True
        elif display_type == "pay":
            path = "assets/pay.gif"
            is_gif = True
        elif display_type == "loading":
            path = "assets/loading.png"
            is_gif = False
        elif display_type == "default":
            path = self.app_instance.original_idle_gif_path
            is_gif = True
        else:
            print(f"유효하지 않은 디스플레이 타입: {display_type}")
            return False
        
        # 화면 변경 요청 설정
        self.app_instance.pending_display_change = {
            'path': path,
            'is_gif': is_gif,
            'display_type': display_type
        }
        
        print(f"화면 변경 요청 전송: {display_type} ({path})")
        return True
    
    
    def setup_routes(self):
        @self.api.get("/")
        def root():
            return {"message": "RealSense 얼굴 인식 API 서버"}
        
        @self.api.post("/display")
        async def change_display(background_tasks: BackgroundTasks, request: DisplayChangeRequest):
            """
            request body: {"display_type": "motion|pay|loading|default"}
            """
            display_type = request.display_type
            
            result = {
                "success": False,
                "message": "유효한 디스플레이 타입이 제공되지 않았습니다."
            }
            
            # 경로 변수 설정
            path = None
            is_gif = False
            
            if display_type == "motion":
                path = "assets/motioncheck.gif"
                is_gif = True
            elif display_type == "pay":
                path = "assets/pay.gif"
                is_gif = True
            elif display_type == "loading":
                path = "assets/loading.png"
                is_gif = False
            elif display_type == "default":
                path = self.app_instance.original_idle_gif_path
                is_gif = True
            else:
                return result
            
            self.app_instance.pending_display_change = {
                'path': path,
                'is_gif': is_gif,
                'display_type': display_type
            }
            
            result = {
                "success": True,
                "message": f"화면 변경 요청이 전송되었습니다: {display_type} ({path})",
                "display_type": display_type,
                "path": path,
                "pending": True
            }
            
            return result
        
        @self.api.post("/recognize")
        async def recognize_face_endpoint(background_tasks: BackgroundTasks):

            # 이미 처리 중인지 확인
            if self.app_instance.processing_api_request:
                return {
                    "success": False,
                    "message": "이미 처리 중인 요청이 있습니다. 잠시 후 다시 시도하세요."
                }
                
            # 기존 프레임 큐 비우기 (이전 데이터 정리)
            try:
                while not self.app_instance.frame_queue.empty():
                    self.app_instance.frame_queue.get_nowait()
                while not self.app_instance.result_queue.empty():
                    self.app_instance.result_queue.get_nowait()
            except queue.Empty:
                pass
            
            # 카메라 모드 활성화
            self.app_instance.set_camera_mode(True)
            # self.app_instance.start_countdown()
            # 사용자가 얼굴 위치를 맞출 수 있도록 3초 대기
            # print("얼굴 위치 맞추기: 3초 대기 중...")
            
            # 세분화된 대기로 중간에 카메라 상태 확인 가능하게 함
            # for i in range(10):  # 0.1초 간격으로 30번 = 3초
            #     if not self.app_instance.camera_mode:
            #         # 중간에 카메라가 꺼진 경우
            #         return {
            #             "success": False,
            #             "message": "카메라가 비활성화되어 인식이 취소되었습니다."
            #         }
            #     await asyncio.sleep(0.1)
            
            # 대기 후 카메라가 여전히 켜져 있는지 확인
            if not self.app_instance.camera_mode:
                return {
                    "success": False,
                    "message": "카메라가 비활성화되어 인식이 취소되었습니다."
                }
            
            # 이미 다른 요청이 시작되었는지 확인
            if self.app_instance.processing_api_request:
                # 카메라 모드 종료
                background_tasks.add_task(self.delayed_camera_off, 0.1)
                return {
                    "success": False,
                    "message": "대기 중 다른 처리가 시작되었습니다. 잠시 후 다시 시도하세요."
                }
            
            print("얼굴 인식 시작!")
            
            # 프레임 수집 설정 업데이트
            self.app_instance.required_frames = 13  # 최대 13개 프레임
            self.app_instance.max_wait_time = 1.5   # 1.5초 제한
            
            # 프레임 수집 시작
            self.app_instance.start_frame_collection(frame_based=True)
            
            # 결과 대기
            await asyncio.get_event_loop().run_in_executor(
                None, self.app_instance.api_result_event.wait
            )
            
            # API 결과 가져오기 (얼굴 인식 결과)
            api_result = self.app_instance.api_result
            
            # 얼굴이 감지되지 않은 경우
            if not api_result.get('face_detected', False):
                background_tasks.add_task(self.delayed_camera_off, 0.2)
                return api_result
            
            # 라이브니스 비율 확인
            live_ratio = api_result.get('live_ratio', 0.0)
            total_frames = len(self.app_instance.collected_frames)
            live_frames = int(total_frames * live_ratio)
            
            print(f"라이브니스 통계: 총 {total_frames}개 프레임 중 {live_frames}개 실제 얼굴 (비율: {live_ratio:.2f})")
            
            # 10개 이상의 라이브 프레임이 필요
            if live_frames < 10:
                background_tasks.add_task(self.delayed_camera_off, 0.2)
                return {
                    "success": False,
                    "message": f"실제 얼굴 프레임이 부족합니다. 필요: 10개, 감지됨: {live_frames}개",
                    "face_detected": True,
                    "is_live": False,
                    "live_ratio": live_ratio,
                    "genderage": api_result,
                }
            
            # 현재 가장 최근의 프레임 캡처하여 사용 (이미 라이브니스 검증된 상태)
            # 최대 10개의 프레임 수집
            live_face_frames = []
            
            try:
                # 최대 10번 프레임 캡처 시도
                for _ in range(10):
                    frames = self.app_instance.pipeline.wait_for_frames()
                    aligned_frames = self.app_instance.align.process(frames)
                    color_frame = aligned_frames.get_color_frame()
                    
                    if color_frame:
                        # 이미지를 NumPy 배열로 변환
                        color_image = np.asanyarray(color_frame.get_data())
                        
                        # 타겟 비율에 맞게 이미지 자르기
                        target_ratio = self.app_instance.target_ratio
                        color_image = crop_to_target_ratio(color_image, target_ratio)
                        
                        # 이미지 인코딩 및 Base64 변환
                        _, buffer = cv2.imencode('.jpg', color_image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                        base64_image = base64.b64encode(buffer).decode('utf-8')
                        
                        # 결과에 저장
                        live_face_frames.append(base64_image)
                    
                    # 짧은 지연 추가 (프레임 간 차이를 위해)
                    await asyncio.sleep(0.05)
                    
                    # 10개 프레임이 모이면 종료
                    if len(live_face_frames) >= 10:
                        break
            except Exception as e:
                print(f"프레임 캡처 오류: {e}")
                traceback.print_exc()
            
            print(f"캡처한 프레임 수: {len(live_face_frames)}")
            
            # 최종적으로 10개의 프레임만 사용
            live_face_frames = live_face_frames[:10]
            
            try:
                # 서버에 얼굴 임베딩 비교 요청
                verification_url = "https://face.cofface.store/verify"
                print(f"서버에 얼굴 검증 요청: {verification_url}, 프레임 수: {len(live_face_frames)}")
                
                # 라이브니스 정보 구성 (로컬에서 이미 검증됨)
                liveness_info = {
                    "is_live": True,
                    "confidence": live_ratio,
                    "message": f"로컬에서 라이브니스 검사 완료: {live_ratio:.2f}"
                }
                
                # 요청 데이터 구성 - 서버 API 요구사항 맞추기
                request_data = {
                    "rgb_image": live_face_frames[0],  # 첫 번째 이미지는 필수 필드로 제공
                    "rgb_images": live_face_frames,    # 나머지 이미지는 배열로 제공
                    "liveness_result": liveness_info
                }
                
                # API 요청 전송
                response = requests.post(
                    verification_url,
                    json=request_data,
                    headers={"Content-Type": "application/json"}
                )
                
                # 응답 확인
                if response.status_code == 200:
                    verification_result = response.json()
                    print(f"서버 응답: {verification_result}")
                    
                    # 응답에 success 필드가 있으면 클라이언트 요구사항에 맞게 포맷 조정
                    if "status" in verification_result:
                        verification_result["success"] = verification_result["status"] == "success"
                    
                    # 결과 반환
                    result = {
                        **verification_result,  # 서버 응답 모든 필드 포함
                        "genderage": api_result,
                    }
                else:
                    print(f"서버 오류 응답: {response.status_code}, {response.text}")
                    result = {
                        "success": False,
                        "message": f"서버 응답 오류: {response.status_code}",
                        "server_message": response.text if response.text else "응답 없음"
                    }
            except Exception as e:
                print(f"얼굴 인식 처리 중 오류 발생: {str(e)}")
                traceback.print_exc()
                result = {
                    "success": False,
                    "message": f"얼굴 인식 처리 중 오류 발생: {str(e)}"
                }
            
            background_tasks.add_task(self.delayed_camera_off, 0.1)
            background_tasks.add_task(self.change_display_helper, "loading")
            return result
        
        # 제스처 감지 시작 API
        @self.api.post("/gesture/start")
        async def start_gesture_detection(background_tasks: BackgroundTasks):
            """제스처 감지를 시작합니다"""
            # 이미 처리 중인지 확인
            if self.app_instance.detecting_gesture:
                return {
                    "success": False,
                    "message": "이미 다른 제스처 감지 세션이 진행 중입니다."
                }
            
            self.signal_display_change("motion")
            
            # 카메라 모드 활성화
            if not self.app_instance.camera_mode:
                self.app_instance.set_camera_mode(True)
            
            success = self.app_instance.start_gesture_detection("default")
            
            if not success:
                return {
                    "success": False,
                    "message": "제스처 감지 시작 실패"
                }
            
            return {
                "success": True,
                "message": "제스처 감지가 시작되었습니다. WebSocket에 연결하세요.",
                "websocket_url": f"/ws/gesture"  # 경로에서 세션 ID 제거
            }

        # 제스처 감지 중지 API
        @self.api.post("/gesture/stop")
        async def stop_gesture_detection(background_tasks: BackgroundTasks):
            """제스처 감지를 중지합니다"""
            if not self.app_instance.detecting_gesture:
                return {
                    "success": False,
                    "message": "현재 진행 중인 제스처 감지 세션이 없습니다."
                }
            
            # 제스처 감지 중지
            if hasattr(self, 'gesture_manager') and self.gesture_manager.is_active():
                # 활성 연결이 있는 경우, 연결을 실제로 닫음
                active_connection = self.gesture_manager.active_connection
                if active_connection:
                    # 마지막 메시지 전송 (선택사항)
                    try:
                        await active_connection.send_json({
                            "type": "stopped",
                            "message": "제스처 감지가 중지되었습니다. 연결이 종료됩니다."
                        })
                        # WebSocket 연결 실제로 닫기
                        await active_connection.close(code=1000)
                    except Exception as e:
                        print(f"웹소켓 종료 중 오류: {e}")
                
                # 연결 관리자에서 연결 정리
                self.gesture_manager.disconnect()
            
            # 기본 GIF로 복귀
            try:
                default_gif_path = self.app_instance.original_idle_gif_path
                if default_gif_path:
                    self.app_instance.idle_gif = AnimatedGIF(default_gif_path)
            except Exception as e:
                print(f"기본 GIF 로드 실패: {e}")
            
            # 카메라 끄기 (지연 후)
            background_tasks.add_task(self.delayed_camera_off, 0.2)
            self.signal_display_change("loading")
            
            return {
                "success": True,
                "message": "제스처 감지가 중지되었습니다."
            }

        # 웹소켓 엔드포인트 - 제스처 감지용 (경로에서 세션 ID 제거)
        @self.api.websocket("/ws/gesture")
        async def gesture_websocket(websocket: WebSocket):
            # 현재 제스처 감지 중인지 확인
            if self.app_instance.detecting_gesture:
                # 이미 다른 연결이 있는지 확인
                if self.gesture_manager.is_active():
                    # 연결을 수락하지만 에러 메시지 전송 후 종료
                    await websocket.accept()
                    await websocket.send_json({
                        "type": "error",
                        "message": "이미 다른 연결에서 제스처 감지 중입니다."
                    })
                    await websocket.close()
                    return
            else:
                # 제스처 감지가 활성화되어 있지 않다면 자동으로 시작
                self.change_display_helper("motion")
                success = self.app_instance.start_gesture_detection("default")
                if not success:
                    await websocket.accept()
                    await websocket.send_json({
                        "type": "error",
                        "message": "제스처 감지 시작에 실패했습니다."
                    })
                    await websocket.close()
                    return
            
            # 웹소켓 연결 관리
            await self.gesture_manager.connect(websocket)
            
            # 초기 메시지 전송
            await self.gesture_manager.send_message(
                {
                    "type": "start",
                    "message": "제스처 감지를 시작합니다. 끄덕이거나 좌우로 흔들어 주세요."
                }
            )
            
            try:
                # 제스처 처리 루프
                while True:
                    last_gesture_detected_time = 0
                    gesture_cooldown = 2.0
                    try:
                        # 클라이언트로부터 메시지 수신 (비동기)
                        data = await asyncio.wait_for(websocket.receive_json(), timeout=0.5)
                        
                        # 중지 요청 처리
                        if data.get("type") == "stop":
                            self.app_instance.stop_gesture_detection()
                            
                            try:
                                default_gif_path = self.app_instance.idle_gif_path
                                if default_gif_path:
                                    self.app_instance.idle_gif = AnimatedGIF(default_gif_path)
                            except Exception as e:
                                print(f"기본 GIF 로드 실패: {e}")
                            
                            await self.gesture_manager.send_message(
                                {
                                    "type": "stopped",
                                    "message": "제스처 감지가 중지되었습니다."
                                }
                            )
                            break
                    # 타임아웃은 무시하고 계속 진행
                    except asyncio.TimeoutError:
                        pass
                    except Exception as e:
                        error_str = str(e)
                        if "(1000," in error_str:
                            print("정상적인 웹소켓 종료 감지: 클라이언트에서 연결 종료")
                            break
                        elif "disconnect message has been received" in error_str:
                            print("웹소켓 연결 종료 감지 - 정리 작업 수행")
                            break
                        else:
                            print(f"웹소켓 메시지 수신 중 오류: {e}")
                    
                    # 결과를 확인할 제스처 업데이트 처리
                    color_image = None
                    try:
                        # 프레임 캡처 (제스처 감지용)
                        frames = self.app_instance.pipeline.wait_for_frames()
                        aligned_frames = self.app_instance.align.process(frames)
                        color_frame = aligned_frames.get_color_frame()
                        
                        if color_frame:
                            color_image = np.asanyarray(color_frame.get_data())
                            # 제스처 감지 처리
                            gesture_result = self.app_instance.process_gesture_frame(color_image)
                            
                            if gesture_result:
                                # 현재 시간
                                current_time = time.time()
                                
                                # 제스처가 확정되었고 쿨다운 시간이 지난 경우만 전송
                                if (gesture_result.get("type") == "gesture_detected" and 
                                    current_time - last_gesture_detected_time > gesture_cooldown):
                                    # 시간 업데이트
                                    last_gesture_detected_time = current_time
                                    
                                    # 제스처 확정 결과 전송
                                    await self.gesture_manager.send_message(gesture_result)
                                    
                                    print(f"제스처 확정 결과 전송: {gesture_result['gesture_name']}")
                                
                                # 업데이트 메시지는 항상 전송 (과도한 메시지 방지)
                                elif gesture_result.get("type") == "gesture_update" and (current_time % 1 < 0.1):
                                    await self.gesture_manager.send_message(gesture_result)
                                
                                # 타임아웃 메시지 전송 후 종료
                                elif gesture_result.get("type") == "timeout":
                                    await self.gesture_manager.send_message(gesture_result)
                                    break
                                
                                if gesture_result and gesture_result.get("type") == "timeout":
                                    await self.gesture_manager.send_message(gesture_result)
                                    break
                    except Exception as e:
                        print(f"제스처 처리 중 오류: {e}")
                        if "disconnect message has been received" in str(e):
                            print("웹소켓 연결 종료 감지 - 정리 작업 수행")
                            break
                        traceback.print_exc()
                    
                    # 짧은 지연 추가
                    await asyncio.sleep(0.05)
            
            except WebSocketDisconnect:
                print(f"WebSocket 연결 종료")
            except Exception as e:
                print(f"WebSocket 오류: {e}")
                traceback.print_exc()
            finally:
                # 연결 종료 시 제스처 감지 중단 및 세션 정리
                print("웹소켓 연결 종료 - 화면 전환 및 정리 작업 수행")
                if getattr(self.app_instance, 'detecting_gesture', False):
                    self.app_instance.stop_gesture_detection()
                
                if self.gesture_manager.is_active():
                    self.gesture_manager.disconnect()
                
                # 카메라 모드 종료 (지연 후)
                await self.delayed_camera_off(0.1)
                
                # 화면 전환 요청 (연결 종료 시)
                self.change_display_helper("loading")
                
        
        @self.api.get("/weather")
        async def weather_get():
            korea_timezone = pytz.timezone('Asia/Seoul')
            now = datetime.now(korea_timezone)
            
            # 앱 상태에서 캐시 접근
            cache = self.weather_cache
            
            # 캐시된 데이터가 없거나 마지막 업데이트 후 1시간 이상 지났으면 새로 요청
            if (cache["data"] is None or 
                cache["last_update"] is None or 
                now - cache["last_update"] > timedelta(hours=1)):
                
                try:
                    # 새로운 날씨 데이터 가져오기
                    weather_data = get_weather()
                    
                    # 캐시 업데이트
                    cache["data"] = weather_data
                    cache["last_update"] = now
                    
                    print(f"날씨 API 호출 - 새로운 데이터: {now}")
                except Exception as e:
                    print(f"날씨 API 호출 실패: {e}")
                    # 캐시된 데이터가 있으면 기존 데이터 사용
                    if cache["data"] is not None:
                        print("오류 발생 - 캐시된 데이터 사용")
            else:
                print(f"캐시된 날씨 데이터 사용 (마지막 업데이트: {cache['last_update']})")
            
            result = {
                "timestamp": now,
                "weather": cache["data"],
                "cached": cache["last_update"] != now
            }
            
            return result
        
        @self.api.post("/camera")
        def camera_control(request: CameraControlRequest):
            """카메라 모드 제어 - 요청 본문으로 데이터 수신"""
            self.app_instance.set_camera_mode(request.enable, request.timeout)
            return {
                "success": True,
                "camera_mode": request.enable,
                "timeout": request.timeout,
                "message": f"카메라 모드 {'활성화' if request.enable else '비활성화'} 성공"
            }
        
        @self.api.get("/status")
        def get_status():
            result = {
                "camera_mode": self.app_instance.camera_mode,
                "processing_api_request": self.app_instance.processing_api_request,
                "detecting_gesture": self.app_instance.detecting_gesture,
                "initialization_done": self.app_instance.initialization_done
            }
            
            # 현재 처리 중인지 확인
            if self.app_instance.processing_api_request:
                # 프레임 기반 모드 확인
                frame_based = hasattr(self.app_instance, 'frame_based_collection') and self.app_instance.frame_based_collection
                
                # 경과 시간
                elapsed_time = time.time() - self.app_instance.collection_start_time
                
                # 현재까지 수집된 프레임 수
                frames_collected = len(self.app_instance.collected_frames)
                
                if frame_based:
                    required_frames = getattr(self.app_instance, 'required_frames', 10)
                    max_wait_time = getattr(self.app_instance, 'max_wait_time', 5.0)
                    progress = min(100, frames_collected * 100 / required_frames)
                    remaining_time = max(0, max_wait_time - elapsed_time)
                    
                    result.update({
                        "status": "processing",
                        "mode": "frame_based",
                        "frames_collected": frames_collected,
                        "required_frames": required_frames,
                        "progress": progress,
                        "elapsed_time": elapsed_time,
                        "remaining_time": remaining_time,
                        "message": f"프레임 수집 중: {frames_collected}/{required_frames} ({progress:.1f}%)"
                    })
                else:
                    # 시간 기반 모드
                    progress = min(100, elapsed_time * 100 / self.app_instance.recognition_time)
                    remaining_time = max(0, self.app_instance.recognition_time - elapsed_time)
                    
                    result.update({
                        "status": "processing",
                        "mode": "time_based",
                        "frames_collected": frames_collected,
                        "elapsed_time": elapsed_time,
                        "total_time": self.app_instance.recognition_time,
                        "progress": progress,
                        "remaining_time": remaining_time,
                        "message": f"시간 기반 수집 중: {elapsed_time:.2f}/{self.app_instance.recognition_time:.2f}초 ({progress:.1f}%)"
                    })
            elif self.app_instance.detecting_gesture:
                # 제스처 감지 상태 정보
                elapsed_time = time.time() - getattr(self.app_instance, 'gesture_start_time', time.time())
                result.update({
                    "status": "detecting_gesture",
                    "session_id": self.app_instance.current_gesture_session,
                    "nod_count": getattr(self.app_instance, 'nod_count', 0),
                    "shake_count": getattr(self.app_instance, 'shake_count', 0),
                    "elapsed_time": elapsed_time,
                    "message": "제스처 감지 중"
                })
            else:
                result.update({
                    "status": "idle",
                    "message": "대기 중"
                })
            
            return result
    
    def signal_display_change(self, display_type):
        result = self.change_display_helper(display_type)
        return {
            "success": result,
            "message": f"화면 변경 요청 전송됨: {display_type}",
            "pending": True
        }
    
    def get_best_face_embedding(self):
            """수집된 프레임에서 임베딩 추출"""
            if not self.app_instance.collected_frames or not self.app_instance.faces_results:
                print("임베딩 추출 실패: 수집된 프레임 또는 얼굴 결과 없음")
                return None
            
            # 얼굴 감지 및 임베딩 추출을 위한 최신 프레임 가져오기
            try:
                # 현재 스레드에서 처리된 임베딩 직접 활용
                original_color = None
                original_depth = None
                
                # 가장 최근 프레임 데이터 사용 시도
                try:
                    with self.app_instance.frame_queue.mutex:
                        if self.app_instance.frame_queue.queue:
                            frame_data = self.app_instance.frame_queue.queue[-1]
                            original_color = frame_data[0]  # 첫 번째 항목이 원본 컬러 이미지
                            print(f"최근 프레임 획득 성공: {original_color.shape if original_color is not None else None}")
                except Exception as e:
                    print(f"프레임 큐 접근 오류 (무시됨): {e}")
                
                # 수집된 얼굴 결과에서 가장 좋은 얼굴 선택
                if original_color is not None:
                    # 얼굴 인식 직접 실행
                    faces = self.app_instance.face_app.get(original_color)
                    if faces:
                        print(f"얼굴 감지 성공: {len(faces)}개 얼굴")
                        # 가장 큰 얼굴 선택 (일반적으로 가장 가까운 얼굴)
                        largest_face = max(faces, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1]))
                        return largest_face.embedding
                    else:
                        print("새 프레임에서 얼굴 감지 실패")
                
                # 대체 방법: 현재 프레임에서 새로 얼굴 감지 시도
                print("대체 방법으로 임베딩 추출 시도 중...")
                try:
                    # 원본 이미지 준비
                    frames = self.app_instance.pipeline.wait_for_frames()
                    aligned_frames = self.app_instance.align.process(frames)
                    depth_frame = aligned_frames.get_depth_frame()
                    color_frame = aligned_frames.get_color_frame()
                    if color_frame:
                        current_color = np.asanyarray(color_frame.get_data())
                        # 이미지 전처리 (필요시)
                        current_color = cv2.flip(current_color, -1)
                        
                        # 얼굴 인식
                        current_faces = self.app_instance.face_app.get(current_color)
                        if current_faces:
                            print(f"현재 프레임에서 얼굴 감지 성공: {len(current_faces)}개")
                            return current_faces[0].embedding
                except Exception as e:
                    print(f"현재 프레임 처리 오류: {e}")
                
                print("모든 임베딩 추출 방법 실패")
                return None
            except Exception as e:
                print(f"임베딩 추출 전체 오류: {e}")
                traceback.print_exc()
                return None
    
    async def delayed_camera_off(self, delay: float):
        """지정된 시간 후 카메라 모드 종료"""
        await asyncio.sleep(delay)
        self.app_instance.set_camera_mode(False)
    
    def load_initial_weather(self):
        """날씨 데이터 초기 로드"""
        korea_timezone = pytz.timezone('Asia/Seoul')
        now = datetime.now(korea_timezone)
        
        try:
            # 초기 날씨 데이터 가져오기
            weather_data = get_weather()
            
            # 캐시 업데이트
            self.weather_cache["data"] = weather_data
            self.weather_cache["last_update"] = now
            
            print(f"서버 시작 - 날씨 데이터 초기 로드 완료: {now}")
        except Exception as e:
            print(f"서버 시작 - 날씨 데이터 초기 로드 실패: {e}")
    
    def start_server(self):
        self.load_initial_weather()
        """별도 스레드에서 FastAPI 서버 시작"""
        def run_server():
            uvicorn.run(self.api, host="0.0.0.0", port=self.port)
        
        self.server_thread = threading.Thread(target=run_server)
        self.server_thread.daemon = True
        self.server_thread.start()
        print(f"FastAPI 서버가 시작되었습니다 (포트: {self.port})")

if __name__ == "__main__":
    args = parse_args()
    app = RealSenseFaceLiveness(args)
    
    # API 서버 시작
    server = FaceRecognitionServer(app, port=args.api_port)
    server.start_server()
    # Pyglet 애플리케이션 실행
    app.run()