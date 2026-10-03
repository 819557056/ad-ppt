"""
Backend configuration file
"""
import os
import sys
from datetime import timedelta

# 基础配置 - 使用更可靠的路径计算方式
# 在模块加载时立即计算并固定路径
_current_file = os.path.realpath(__file__)  # 使用realpath解析所有符号链接
BASE_DIR = os.path.dirname(_current_file)
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_FRONTEND_PORT = 3011
DEFAULT_BACKEND_PORT = 5011

# Flask配置
class Config:
    """Base configuration"""
    SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-change-this')
    
    # 数据库配置
    # Use absolute path to avoid WSL path issues
    db_path = os.path.join(BASE_DIR, 'instance', 'database.db')
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL', 
        f'sqlite:///{db_path}'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # SQLite线程安全配置 - 关键修复
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 3600,
        **({'connect_args': {'check_same_thread': False, 'timeout': 30}}
           if SQLALCHEMY_DATABASE_URI.startswith('sqlite:') else {}),
    }
    SCENE_EDITOR_ENABLED = os.getenv('SCENE_EDITOR_ENABLED', 'false').lower() == 'true'
    ASSET_STORE_ROOT = os.getenv('ASSET_STORE_ROOT', os.path.join(PROJECT_ROOT, 'scene_assets'))
    MAX_GENERATED_PAGES = int(os.getenv('MAX_GENERATED_PAGES', '30'))
    MAX_UPLOAD_BYTES = int(os.getenv('MAX_UPLOAD_BYTES', str(50 * 1024 * 1024)))
    MODEL_GATEWAY_ALLOWLIST = os.getenv('MODEL_GATEWAY_ALLOWLIST', '')
    CREDENTIAL_ENCRYPTION_KEY = os.getenv('CREDENTIAL_ENCRYPTION_KEY', '')
    CREDENTIAL_ENCRYPTION_KEY_ID = os.getenv('CREDENTIAL_ENCRYPTION_KEY_ID', 'v1')
    OWNER_MODEL_CONCURRENCY = int(os.getenv('OWNER_MODEL_CONCURRENCY', '2'))
    SCENE_STORAGE_QUOTA_BYTES = int(os.getenv('SCENE_STORAGE_QUOTA_BYTES', str(20 * 1024 ** 3)))
    OWNER_STORAGE_QUOTA_BYTES = int(os.getenv('OWNER_STORAGE_QUOTA_BYTES', str(5 * 1024 ** 3)))
    SCENE_STORAGE_LOCK_SECONDS = int(os.getenv('SCENE_STORAGE_LOCK_SECONDS', '10'))
    SCENE_STORAGE_MAX_ENTRIES = int(os.getenv('SCENE_STORAGE_MAX_ENTRIES', '200000'))
    # Only consumed by the explicitly offline retention command; never an automatic GC.
    SCENE_RETENTION_CANDIDATE_DAYS = int(os.getenv('SCENE_RETENTION_CANDIDATE_DAYS', '30'))
    SCENE_RETENTION_TASK_DAYS = int(os.getenv('SCENE_RETENTION_TASK_DAYS', '90'))
    SCENE_RETENTION_ATTEMPT_DAYS = int(os.getenv('SCENE_RETENTION_ATTEMPT_DAYS', '180'))
    SCENE_RETENTION_ASSET_DAYS = int(os.getenv('SCENE_RETENTION_ASSET_DAYS', '30'))
    SCENE_RETENTION_ORPHAN_HOURS = int(os.getenv('SCENE_RETENTION_ORPHAN_HOURS', '24'))
    SCENE_QUEUE_CAPACITY_UNITS = int(os.getenv('SCENE_QUEUE_CAPACITY_UNITS', '1024'))
    OWNER_QUEUE_CAPACITY_UNITS = int(os.getenv('OWNER_QUEUE_CAPACITY_UNITS', '256'))
    MAX_TEMPLATE_PAGES = int(os.getenv('MAX_TEMPLATE_PAGES', '50'))
    MAX_UNPACKED_BYTES = int(os.getenv('MAX_UNPACKED_BYTES', str(500 * 1024 * 1024)))
    SCENE_RENDER_JOB_ROOT = os.getenv('SCENE_RENDER_JOB_ROOT', '')
    # The isolated renderer runs as a different UID in docker-compose.scene.yml.
    SCENE_RENDER_UID = int(os.getenv('SCENE_RENDER_UID', '-1'))
    SCENE_RENDER_GID = int(os.getenv('SCENE_RENDER_GID', '-1'))
    SCENE_RENDER_UNIQUE_UID = os.getenv('SCENE_RENDER_UNIQUE_UID', 'false').lower() == 'true'
    SCENE_RENDER_UID_BASE = int(os.getenv('SCENE_RENDER_UID_BASE', '10000'))
    RENDER_TIMEOUT_SECONDS = int(os.getenv('RENDER_TIMEOUT_SECONDS', '180'))
    SCENE_LEASE_SECONDS = int(os.getenv('SCENE_LEASE_SECONDS', '60'))
    SCENE_HEARTBEAT_SECONDS = int(os.getenv('SCENE_HEARTBEAT_SECONDS', '10'))
    SCENE_READY_HEARTBEAT_SECONDS = int(os.getenv('SCENE_READY_HEARTBEAT_SECONDS', '30'))
    SCENE_MIN_FREE_BYTES = int(os.getenv('SCENE_MIN_FREE_BYTES', str(100 * 1024 * 1024)))
    
    # 文件存储配置
    UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, 'uploads')
    MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # 200MB max file size
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ALLOWED_REFERENCE_FILE_EXTENSIONS = {'pdf', 'docx', 'pptx', 'doc', 'ppt', 'xlsx', 'xls', 'csv', 'txt', 'md'}
    
    # AI服务配置
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', '')
    GOOGLE_API_BASE = os.getenv('GOOGLE_API_BASE') or ''
    
    # Provider format: gemini | openai | volcengine | vertex | lazyllm
    AI_PROVIDER_FORMAT = os.getenv('AI_PROVIDER_FORMAT', 'gemini')

    # Google Cloud Vertex AI (requires AI_PROVIDER_FORMAT=vertex)
    VERTEX_PROJECT_ID = os.getenv('VERTEX_PROJECT_ID', '')
    VERTEX_LOCATION = os.getenv('VERTEX_LOCATION', 'us-central1')
    
    # GenAI (Gemini) 格式专用配置
    GENAI_TIMEOUT = float(os.getenv('GENAI_TIMEOUT', '300.0'))  # Gemini 超时时间（秒）
    GENAI_MAX_RETRIES = int(os.getenv('GENAI_MAX_RETRIES', '2'))  # Gemini 最大重试次数（应用层实现）
    
    # OpenAI 格式专用配置（当 AI_PROVIDER_FORMAT=openai 时使用）
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')  # 当 AI_PROVIDER_FORMAT=openai 时必须设置
    OPENAI_API_BASE = os.getenv('OPENAI_API_BASE') or 'https://api.inferera.com/v1'
    OPENAI_TIMEOUT = float(os.getenv('OPENAI_TIMEOUT', '480.0'))  # 8 分钟：留出 gpt-image-2 生图(~225s)+传输的余量
    OPENAI_MAX_RETRIES = int(os.getenv('OPENAI_MAX_RETRIES', '2'))  # 减少重试次数，避免过多重试导致累积超时

    # 火山方舟 Agent Plans（OpenAI-compatible）
    VOLCENGINE_API_KEY = os.getenv('VOLCENGINE_API_KEY', '') or os.getenv('ARK_API_KEY', '')
    VOLCENGINE_API_BASE = os.getenv('VOLCENGINE_API_BASE') or 'https://ark.cn-beijing.volces.com/api/plan/v3'

    # Anthropic 格式专用配置（当 AI_PROVIDER_FORMAT=anthropic 时使用）
    # 支持 ANTHROPIC_AUTH_TOKEN 作为 ANTHROPIC_API_KEY 的别名
    # 支持 ANTHROPIC_BASE_URL 作为 ANTHROPIC_API_BASE 的别名
    ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '') or os.getenv('ANTHROPIC_AUTH_TOKEN', '')
    ANTHROPIC_API_BASE = os.getenv('ANTHROPIC_API_BASE', '') or os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
    ANTHROPIC_VERSION = os.getenv('ANTHROPIC_VERSION', '2023-06-01')
    ANTHROPIC_MAX_TOKENS = int(os.getenv('ANTHROPIC_MAX_TOKENS', '8192'))

    # Lazyllm 格式专用配置（当 AI_PROVIDER_FORMAT=lazyllm 时使用）
    TEXT_MODEL_SOURCE = os.getenv('TEXT_MODEL_SOURCE', '')                   # 文本生成模型厂商（留空则跟随全局 AI_PROVIDER_FORMAT）
    IMAGE_MODEL_SOURCE = os.getenv('IMAGE_MODEL_SOURCE', '')                   # 图片生成模型厂商（留空则跟随全局 AI_PROVIDER_FORMAT）
    IMAGE_CAPTION_MODEL_SOURCE = os.getenv('IMAGE_CAPTION_MODEL_SOURCE', '')   # 图片识别模型厂商（留空则跟随全局 AI_PROVIDER_FORMAT）

    # 各模型类型的独立 API 配置（优先级高于全局配置）
    # 文本模型独立配置
    TEXT_API_KEY = os.getenv('TEXT_API_KEY', '')
    TEXT_API_BASE = os.getenv('TEXT_API_BASE', '')
    # 图像模型独立配置
    IMAGE_API_KEY = os.getenv('IMAGE_API_KEY', '')
    IMAGE_API_BASE = os.getenv('IMAGE_API_BASE', '')
    # 图片识别模型独立配置
    IMAGE_CAPTION_API_KEY = os.getenv('IMAGE_CAPTION_API_KEY', '')
    IMAGE_CAPTION_API_BASE = os.getenv('IMAGE_CAPTION_API_BASE', '')
    
    # AI 模型配置
    TEXT_MODEL = os.getenv('TEXT_MODEL', 'gemini-3-flash-preview')
    IMAGE_MODEL = os.getenv('IMAGE_MODEL', 'gemini-3-pro-image-preview')

    # MinerU 文件解析服务配置
    MINERU_TOKEN = os.getenv('MINERU_TOKEN', '')
    MINERU_API_BASE = os.getenv('MINERU_API_BASE', 'https://mineru.net')
    
    # 图片识别模型配置
    IMAGE_CAPTION_MODEL = os.getenv('IMAGE_CAPTION_MODEL', 'gemini-3-flash-preview')
    
    # 并发配置
    MAX_DESCRIPTION_WORKERS = int(os.getenv('MAX_DESCRIPTION_WORKERS', '20'))
    MAX_IMAGE_WORKERS = int(os.getenv('MAX_IMAGE_WORKERS', '20'))
    
    # 图片生成配置
    DEFAULT_ASPECT_RATIO = "16:9"
    DEFAULT_RESOLUTION = "2K"
    # 图片质量档位（OpenAI 兼容图片模型: auto/low/medium/high/xhigh/max）
    # xhigh / max 需要 gpt-image-2.5 及更新模型，旧模型会自动回落到 high
    IMAGE_QUALITY = os.getenv('IMAGE_QUALITY', 'auto')
    
    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    WERKZEUG_LOG_LEVEL = (os.getenv('WERKZEUG_LOG_LEVEL') or 'INFO').strip().upper()
    
    # CORS配置
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', f'http://localhost:{DEFAULT_FRONTEND_PORT}').split(',')
    
    # 输出语言配置
    # 可选值: 'zh' (中文), 'ja' (日本語), 'en' (English), 'auto' (自动)
    OUTPUT_LANGUAGE = os.getenv('OUTPUT_LANGUAGE', 'zh')
    
    # 火山引擎配置
    VOLCENGINE_ACCESS_KEY = os.getenv('VOLCENGINE_ACCESS_KEY', '')
    VOLCENGINE_SECRET_KEY = os.getenv('VOLCENGINE_SECRET_KEY', '')
    VOLCENGINE_INPAINTING_TIMEOUT = int(os.getenv('VOLCENGINE_INPAINTING_TIMEOUT', '60'))  # Inpainting 超时时间（秒）
    VOLCENGINE_INPAINTING_MAX_RETRIES = int(os.getenv('VOLCENGINE_INPAINTING_MAX_RETRIES', '3'))  # 最大重试次数

    # Inpainting Provider 配置（用于 InpaintingService 的单张图片修复）
    # 可选值: 'volcengine' (火山引擎), 'gemini' (Google Gemini)
    # 注意: 可编辑PPTX导出功能使用 ImageEditabilityService，其中 HybridInpaintProvider 会结合百度重绘和生成式质量增强
    INPAINTING_PROVIDER = os.getenv('INPAINTING_PROVIDER', 'gemini')  # 默认使用 Gemini

    # 百度 API 配置（用于 OCR 和图像修复）
    BAIDU_API_KEY = os.getenv('BAIDU_API_KEY', '') or os.getenv('BAIDU_OCR_API_KEY', '')

    # TTS 视频导出配置
    TTS_DEFAULT_VOICE_ZH = os.getenv('TTS_DEFAULT_VOICE_ZH', 'zh-CN-XiaoxiaoNeural')
    TTS_DEFAULT_VOICE_EN = os.getenv('TTS_DEFAULT_VOICE_EN', 'en-US-JennyNeural')
    TTS_DEFAULT_VOICE_JA = os.getenv('TTS_DEFAULT_VOICE_JA', 'ja-JP-NanamiNeural')
    TTS_DEFAULT_RATE = os.getenv('TTS_DEFAULT_RATE', '+0%')
    VIDEO_OUTPUT_WIDTH = int(os.getenv('VIDEO_OUTPUT_WIDTH', '1920'))
    VIDEO_OUTPUT_HEIGHT = int(os.getenv('VIDEO_OUTPUT_HEIGHT', '1080'))
    VIDEO_FPS = int(os.getenv('VIDEO_FPS', '25'))
    FFMPEG_PATH = os.getenv('FFMPEG_PATH', 'ffmpeg')
    DEFAULT_SILENT_CLIP_DURATION = float(os.getenv('DEFAULT_SILENT_CLIP_DURATION', '3.0'))


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True


class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False


# 根据环境变量选择配置
config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}

def get_config():
    """Get configuration based on environment"""
    env = os.getenv('FLASK_ENV', 'development')
    return config_map.get(env, DevelopmentConfig)
